/* ACTIVITY: the section's list in the left pane is today's events, newest
   first, one row each, and every row opens its thread's conversation in the middle pane.
   The page in the middle (nothing selected) is the dispatcher heartbeat's age, today's counts
   (lit / answered / settled / runs, computed here from /api/triage/events since local
   midnight), the held-by-gate count (home), and the /api/status summary as the right pane.
   A heartbeat that was never published (404) says so; it is not an error. Redraws every 60 s
   while open and on R.
   L7, not a dashboard: each thread's newest row carries what can be done from here
   (answer, snooze, wake, retry: web/actions.js), and the page lists the runs that went quiet
   (dispatched and never reported, or out of attempts) with a Retry on each.
   It also says where the last 7 days' cards came from, and puts an alert at the top when
   runs are filing their own work. Whatever section is open, the
   ACTIVITY row's count turns red when a problem is bad, re-read every 60 s ().      */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const root = $("#view-activity"), list = $("#list-activity");
  let timer = null, events = [], evErr = "";

  const midnight = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
  const dur = (s) => { s = Math.max(0, Math.round(s)); if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s"; if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m"; return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h"; };
  const clock = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";

  // What an event means for today's tally. Kinds are the store's own (helm/store.py add_event).
  function tally(events) {
    const c = { lit: 0, answered: 0, settled: 0, runs: 0 };
    for (const e of events) {
      if (e.kind === "bot-ask" || e.kind === "filed") c.lit++;
      else if (e.kind === "owner-reply") c.answered++;
      else if (e.kind === "bot-result") c.runs++;
      else if (e.kind === "state-change" && (e.reply_kind === "close" || /\b(settled|closed|done)\b/i.test(e.body || ""))) c.settled++;
      else if (e.kind === "unbacklog") c.lit++;   // pulled back: it asks again
    }
    return c;
  }
  const cap = (x) => { x = String(x || ""); return x.charAt(0).toUpperCase() + x.slice(1); };
  const rowKind = (e) => e.kind === "owner-reply" || e.kind === "owner-note" ? ["answered", e.kind === "owner-note" ? "your note" : "you answered"]
    : e.kind === "bot-result" ? ["run", "report"] : /-attachment$/.test(e.kind || "") ? ["run", "attached"] : e.kind === "bot-ask" || e.kind === "filed" ? ["lit", e.kind === "filed" ? "filed" : "asked you"]
    : e.kind === "state-change" && (e.reply_kind === "close" || /\b(settled|closed|done)\b/i.test(e.body || "")) ? ["settled", "settled"]
    : ["state", String(e.kind || "").replace(/-/g, " ")];

  // Ledger : the ACTIVITY row carries a 'bad' mark whenever a problem is bad, read from
  // /api/status every 60 s whichever section is open, so a dispatcher that stopped or a stuck
  // queue is seen from the list and not only here (the earlier app's pip on the tab).
  const HB_BAD_S = 900;
  const aged = (hb) => hb && hb.ts ? Object.assign({}, hb, { age_s: Date.now() / 1000 - Number(hb.ts) }) : null;
  // Work's status carries the heartbeat with its age; home's does not, so it is read (or passed) raw.
  const hbOf = (st, raw) => st && st.dispatcher && typeof st.dispatcher === "object" ? (st.dispatcher.known ? st.dispatcher : null) : aged(raw);
  function problems(st, hb) {
    const out = [], h = (st && st.health) || {}, q = h.queue || {}, cr = h.created || {};
    const n = (k, one, many) => k + " " + (k === 1 ? one : many);
    if (hb && hb.age_s > HB_BAD_S) out.push("The dispatcher has stopped ticking: last heard " + dur(hb.age_s) + " ago");
    if (hb && hb.state === "blocked") out.push("The dispatcher is blocked: " + (hb.reason || "unknown"));
    if (q.gave_up) out.push(n(q.gave_up, "answer", "answers") + " given up on");
    if (q.stale) out.push(n(q.stale, "run", "runs") + " went quiet");
    if (q.pending && q.oldest_wait_s > 1800) out.push(`Answers are backing up: ${q.pending} waiting, the oldest for ${dur(q.oldest_wait_s)}`);
    if (cr.runs && cr.replacement_rate >= 1) out.push("Runs are creating their own work");
    return out;
  }
  const markRow = (st, hb) => Helm.setAlarm("activity", problems(st, hb).join("\n"));
  async function pollHealth() {
    if (document.hidden) return;
    try {
      const st = await api("/api/status");
      let raw = null;
      if (!(st && st.dispatcher)) { try { raw = await api("/api/triage/dispatcher-heartbeat"); } catch { /* never published */ } }
      markRow(st, hbOf(st, raw));
    } catch { /* status unreachable: the mark stays as last read */ }
  }

  async function loadEvents() {
    try { const r = await api("/api/triage/events?since=" + midnight() + "&limit=5000"); events = r.events || []; evErr = ""; }
    catch (e) { evErr = e.message; }
    Helm.setCount("activity", events.length);
  }

  // The left pane: today's events, newest first, each a row into its conversation.
  function drawList() {
    const on = Helm.route.view === "activity" ? Helm.route.tid : null;
    list.replaceChildren();
    const ttl = el("header", "list-title"); ttl.append(el("h2", null, "Activity"), el("p", "hm-meta", `Today, ${events.length} event${events.length === 1 ? "" : "s"}`));
    list.append(ttl);
    const h = el("div", "lhead"); h.append(el("span", null, `Since midnight`));
    // On a phone the summary page is behind the list: one link to it.
    const sum = el("a", "only-phone act-sumlink", "Summary"); sum.href = "#/activity?summary=1"; sum.title = "Dispatcher heartbeat, today's counts, runs that went quiet";
    h.append(sum); list.append(h);
    if (evErr) list.append(el("div", "lempty", "Events unreachable: " + evErr));
    else if (!events.length) list.append(Helm.emptyNode("Quiet so far today", "Nothing has moved yet. Asks, answers and reports show up here as they happen.", { glyph: "activity" }));
    const threads = (typeof state !== "undefined" && state.threads) || [];
    const barred = new Set();
    for (const e of events.slice().reverse().slice(0, 300)) {
      const [cls, label] = rowKind(e);
      const a = el("a", "lrow " + (e.state || "") + (e.thread_id === on ? " on" : ""));
      a.href = Helm.convHref(e.thread_id, "activity"); a.dataset.id = e.thread_id;
      a.append(el("span", "bar"));
      const main = el("div");
      main.append(el("div", "ltitle", e.title || e.thread_id));
      const meta = el("div", "lmeta"); meta.append(el("span", "k " + cls, cap(label))); if (e.source) meta.append(el("span", null, e.source)); main.append(meta);
      if (e.body && e.kind !== "bot-ask") main.append(el("div", "lask", Helm.humanBody(e.kind, e.body).slice(0, 160)));
      a.append(main, el("span", "lage", clock(e.ts)));
      // The thread's actions ride on its newest row only (one set per thread, not per event).
      if (typeof HelmActions !== "undefined" && !barred.has(e.thread_id)) {
        barred.add(e.thread_id);
        const t = threads.find(x => x.thread_id === e.thread_id) || { thread_id: e.thread_id, state: e.state };
        const items = HelmActions.forThread(t, "activity").filter(it => it.key !== "open");   // the row itself opens it
        if (items.length) { const bar = HelmActions.bar(items); bar.classList.add("compact"); main.append(bar); }
      }
      list.append(a);
    }
  }

  // Today as a log (helm.css .hm-log): the time in the gutter, a node on the rule, who did what, and the thread as a chip.
  function drawDayLog(view) {
    const sec = el("section", "act-daylog");
    sec.append(el("h3", "act-h", "Today's log"));
    const LOGMAX = 25;
    const evs = events.slice().reverse().slice(0, LOGMAX);
    if (!evs.length) { sec.append(Helm.emptyNode("Quiet so far today", "Nothing has moved yet. Asks, answers and reports show up here as they happen.", { glyph: "activity" })); view.append(sec); return; }
    const ol = el("ol", "hm-log");
    const now = el("li", "hm-log-now"); now.append(el("span", "hm-log-t", clock(Date.now() / 1000)), el("i"), el("b")); ol.append(now);
    // a generic source ("bot", "agent", "helm") says nothing: only a named one prefixes the line
    const WHO = (e) => { const w = String(e.source || "").split(":")[0]; return w && !/^(owner|bot|agent|helm|system|worker)$/i.test(w) ? w : ""; };
    for (const e of evs) {
      const [cls, label] = rowKind(e);
      const glyph = cls === "lit" ? "ask" : cls === "answered" ? "pen" : cls === "settled" ? "check" : cls === "run" ? "run" : "thread";
      const li = el("li", "hm-log-entry t-" + (cls === "lit" ? "ask" : cls === "run" ? "run" : cls === "answered" ? "ask" : "thread"));
      const nd = el("div", "hm-log-nd"); const i = el("i"); i.append(Helm.glyph(glyph, 13)); nd.append(i);
      const bd = el("div", "hm-log-bd");
      const line = el("p", "act-daylog-line");
      const who = WHO(e);
      line.append(el("span", "act-daylog-what", cls === "run" ? (who ? who + " reported" : "Reported") : who && cls === "lit" ? who + " " + label : cap(label)), " ");
      const href = Helm.convHref(e.thread_id, "activity");
      line.append(typeof Helm.chip === "function" ? Helm.chip({ type: cls === "lit" ? "ask" : "thread", id: e.thread_id, label: e.title || e.thread_id, href }) : Object.assign(el("a", null, e.title || e.thread_id), { href }));
      bd.append(line);
      if (e.body && e.kind !== "bot-ask") bd.append(el("p", "hm-meta act-daylog-body", Helm.humanBody(e.kind, e.body).slice(0, 180)));
      li.append(el("div", "hm-log-t", clock(e.ts)), nd, bd);
      ol.append(li);
    }
    sec.append(ol);
    if (events.length > LOGMAX) {
      const more = el("p", "hm-meta act-daylog-more", `and ${events.length - LOGMAX} more earlier today. `);
      const a = el("a", null, "See them all in the list"); a.href = "#/activity";
      more.append(a); sec.append(more);
    }
    view.append(sec);
  }

  // The middle pane (nothing selected): heartbeat, today's counts, the gate; the status as properties.
  async function drawPage() {
    const view = el("div", "view");
    view.append(el("h2", "agents-title", "The day so far"));
    root.replaceChildren(view);
    // Extra bands lead the page: H.hooks.activityBands, a no-op until something adds one.
    for (const n of Helm.hooks.activityBands.run({ view, route: Helm.route })) view.append(n);
    const alerts = el("div", "act-alerts"); view.append(alerts);   // filled once /api/status is read
    const grid = el("div", "act-grid"); view.append(grid);
    // A tile with somewhere to go is a link there (L7: the page is not a dashboard).
    const tile = (n, label, cls, sub, href) => {
      const t = el(href ? "a" : "div", "act-tile" + (href ? " act-link" : ""));
      if (href) { t.href = href; t.title = "Open " + label; }
      t.append(el("div", "n " + (cls || ""), n), el("div", "l", label));
      if (sub) { const s = el("div", "s"); if (typeof sub === "string") s.textContent = sub; else s.append(sub); t.append(s); }
      grid.append(t); return t;
    };
    const today = (() => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); })();

    // Dispatcher heartbeat: the age is the signal; a 404 means it was never published.
    let hb = null, hbErr = "";
    try { hb = await api("/api/triage/dispatcher-heartbeat"); }
    catch (e) { hbErr = /^404/.test(e.message) ? "never published" : e.message; }
    if (hb && hb.ts) {
      const age = Date.now() / 1000 - Number(hb.ts);
      const short = age < 60 ? Math.round(age) + "s" : age < 3600 ? Math.round(age / 60) + "m" : age < 86400 ? Math.round(age / 3600) + "h" : Math.round(age / 86400) + "d";
      tile(short + " ago", "Dispatcher heartbeat", age > HB_BAD_S ? "bad" : age > 300 ? "warn" : "ok", (hb.host ? `from ${hb.host} · ` : "") + "exactly " + dur(age));
    } else tile("—", "Dispatcher heartbeat", "warn", hbErr || "no timestamp in the heartbeat");

    const c = tally(events);
    tile(String(c.lit), "Asked you today", c.lit ? "lit" : "", "Cards that asked you something", "#/needs-you");
    tile(String(c.answered), "You answered", c.answered ? "blue" : "", "Your taps and notes today", Helm.threadsHref({ from: today }));
    tile(String(c.settled), "Settled", c.settled ? "ok" : "", "Closed or done today", Helm.threadsHref({ state: "done", from: today }));
    tile(String(c.runs), "Agent reports", c.runs ? "" : "", "Runs that reported back today", Helm.threadsHref({ from: today }));

    // Held by gate: what the needs-owner gate kept from him in the last 14 days.
    if (true) {
      try {
        const g = await api("/api/triage/gate?days=14");
        const kept = (g.entries || []).filter(x => x.decision === "held" || x.decision === "settled");
        const link = el("a", null, "See them under Threads"); link.href = Helm.threadsHref({ gate: "1" });
        tile(String(kept.length), "Held by the gate, 14 days", kept.length ? "warn" : "", kept.length ? link : "Nothing kept from you");
      } catch { /* the gate route needs the store's gate; nothing to show */ }
    }
    drawDayLog(view);
    view.append(el("p", "muted", "Today's events are the rows on the left; each one opens its conversation. The status is on the right."));

    // /api/status: the store's own health summary, as the page's properties.
    let st = null;
    try { st = await api("/api/status"); } catch (e) { tile("—", "status", "bad", e.message); }
    if (st) markRow(st, hbOf(st, hb));
    // Runs that went quiet: each can be retried right here (the store's own test, not an age guess).
    const quiet = (st && st.quiet_runs) || [];
    if (typeof HelmActions !== "undefined") { HelmActions.quiet = new Set(quiet.map(q => q.thread_id)); drawList(); }
    const qBox = el("section", "act-quiet");
    qBox.append(el("h3", null, quiet.length ? `Runs that went quiet · ${quiet.length}` : "Runs that went quiet"));
    if (!quiet.length) qBox.append(el("p", "muted", st ? "None: every dispatched run has reported back." : "Status unreachable."));
    for (const q of quiet) {
      const row = el("div", "act-quiet-row");
      const link = el("a", "act-quiet-title", q.title || q.thread_id); link.href = Helm.convHref(q.thread_id, "activity");
      const since = q.since ? " · since " + clock(q.since) : "";
      row.append(link, el("span", "muted", q.why + since));
      if (typeof HelmActions !== "undefined") {
        row.append(HelmActions.bar([{ key: "retry", label: "Retry", title: "Run it again; the attempt budget starts over",
          act: (b) => HelmActions.run(b, "Sent back to run again", () => HelmActions.helm(q.thread_id, "retry", {}), { thread_id: q.thread_id }) }]));
      }
      qBox.append(row);
    }
    view.append(qBox);
    if (st && Array.isArray(st.runs)) drawRuns(view, st);
    if (st && st.health && st.health.created) drawCreated(view, alerts, st.health.created);
    if (st) {
      const h = st.health || {};
      const by = h.by_state || {};
      const kv = el("div", "act-kv");
      // A count links to the THREADS view that lists exactly those threads.
      const add = (k, v, state, title) => {
        if (v == null || v === "") return;
        const key = el("span", "k", k); if (title) key.title = title;
        let val = el("span", null, String(v));
        if (state) { val = el("a", "act-link", String(v)); val.href = Helm.threadsHref({ state }); }
        kv.append(key, val);
      };
      // Epoch seconds in a status blob read as a time of day (work's dispatcher fields).
      const when = (v) => (typeof v === "number" && v > 1e9 && v < 4e9) ? new Date(v * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " (" + dur(Date.now() / 1000 - v) + " ago)" : v;
      add("needs you", h.needs_you, "needs-you"); add("open", h.in_flight, null, "Not done, not parked, not in the backlog: needs you, in flight and on hold together");
      add("parked", h.parked); add("snoozed", h.snoozed);
      add("on hold", by["waiting-external"], "waiting-external"); add("in flight", by.working, "working"); add("backlog", by.backlog, "backlog"); add("done", by.done, "done");
      add("rechecks due", h.rechecks_due);
      if (h.queue) add("dispatch queue", `${h.queue.pending} pending · ${h.queue.gave_up} gave up · ${h.queue.stale} stale` + (h.queue.oldest_wait_s ? ` · oldest ${dur(h.queue.oldest_wait_s)}` : ""));
      add("threads", h.threads_total); add("suppressions", st.suppressions);
      if (st.db_age_s != null) add("db written", dur(st.db_age_s) + " ago");
      if (st.dispatcher && typeof st.dispatcher === "object") {
        // One row per field: a raw JSON blob in one span is wider than a phone (412 px).
        for (const [k, v] of Object.entries(st.dispatcher)) add("dispatcher " + k, v && typeof v === "object" ? JSON.stringify(v) : when(v));
      } else if (st.dispatcher) add("dispatcher", st.dispatcher);
      const box = el("div", "act-list act-props"); box.append(kv);
      if (Helm.route.view === "activity" && !Helm.route.tid) Helm.setProps("Status", box);
    }
  }

  // The executor runs, what they cost, the threads that cost most and the dispatcher's log,
  // as the poller last sent them on its heartbeat.
  const NOT_SENT = "the poller has not sent run data yet";
  const usd = (v) => "$" + Number(v || 0).toFixed(2);
  const tok = (n) => n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(0) + "k" : String(n || 0);
  function drawRuns(view, st) {
    const box = (cls, title) => { const b = el("section", "act-quiet " + cls); b.append(el("h3", null, title)); view.append(b); return b; };
    const convLink = (tid, title) => { const a = el("a", "act-quiet-title", title || tid); a.href = Helm.convHref(tid, "activity"); return a; };
    const costs = st.costs || {}, all = costs.all;

    const rb = box("act-runs", "Executor runs");
    if (!st.runs.length) rb.append(el("p", "muted", NOT_SENT));
    for (const r of st.runs) {
      const row = el("div", "act-quiet-row");
      row.append(r.thread_id ? convLink(r.thread_id, r.title) : el("span", "act-quiet-title", r.name));
      const bits = [clock(r.ts), r.outcome === "result" ? "filed a result" : r.outcome === "raw-kept" ? "raw output kept" : r.outcome === "no-output" ? "no output" : r.outcome,
        r.turns + " turn" + (r.turns === 1 ? "" : "s"), usd(r.cost_usd)];
      if (r.duration_s) bits.push(dur(r.duration_s));
      row.append(el("span", "muted", bits.filter(Boolean).join(" · ")));
      row.title = r.name;
      rb.append(row);
    }

    const cb = box("act-costs", "What the runs cost");
    if (!all) cb.append(el("p", "muted", NOT_SENT));
    else {
      const kv = el("div", "act-kv");
      const add = (k, v) => kv.append(el("span", "k", k), el("span", null, v));
      const line = (a) => a.runs + " run" + (a.runs === 1 ? "" : "s") + ", " + usd(a.cost_usd) + " nominal" + (a.runs ? ", " + Math.round(a.turns_per_run) + " turns each" : "");
      const toks = (a) => a.tokens ? tok(a.tokens) + " (" + Math.round(100 * a.cache_read / a.tokens) + "% re-read context, " + (100 * a.output / a.tokens).toFixed(1) + "% written)" : "none";
      const w = costs.window || {};
      add("runs, last " + costs.window_days + " days", line(w));
      add("tokens, last " + costs.window_days + " days", toks(w));
      add("runs, last " + costs.kept_days + " days", line(all));
      add("tokens, last " + costs.kept_days + " days", toks(all));
      cb.append(kv, el("p", "muted", "Nominal = API list price of what each run consumed, read from its own output. Runs draw on the plan meter, not a bill; this is the comparable number."));
    }

    const tb = box("act-top", "Top threads by cost · " + (costs.window_days || 7) + " days");
    if (!all) tb.append(el("p", "muted", NOT_SENT));
    else if (!(costs.top || []).length) tb.append(el("p", "muted", "No runs in the last " + costs.window_days + " days."));
    for (const t of costs.top || []) {
      const row = el("div", "act-quiet-row");
      row.append(convLink(t.thread_id, t.title), el("span", "muted", t.runs + " run" + (t.runs === 1 ? "" : "s") + " · " + usd(t.cost_usd)));
      tb.append(row);
    }

    const lb = box("act-log", "Dispatcher log");
    const lines = (st.dispatcher_log || []).slice(-14);
    if (!lines.length) lb.append(el("p", "muted", NOT_SENT));
    else { const pre = el("div", "act-logtail"); for (const ln of lines) pre.append(el("div", null, ln)); lb.append(pre); }
  }

  // Ledger , : where the last 7 days' cards came from (the store's origin
  // accounting, helm/store.py origins), and an alert at the top when runs file their own work:
  // new cards per run at 0.5 or more is where to start worrying, 1 or more is a loop.
  function drawCreated(view, alerts, cr) {
    const rate = Number(cr.replacement_rate || 0);
    if (cr.runs && rate >= 0.5) {
      const a = el("div", "act-alert " + (rate >= 1 ? "bad" : "warn"));
      a.append(el("b", null, "Runs are creating their own work"),
        el("div", null, `${cr.by_run} of the ${cr.created} cards created in the last 7 days were filed by runs, over ${cr.runs} run${cr.runs === 1 ? "" : "s"} (${rate.toFixed(2)} new per run).`),
        el("div", "muted", "Find the producer re-ingesting its own output."));
      alerts.append(a);
    }
    const b = el("section", "act-quiet act-created"); b.append(el("h3", null, "Where cards came from, last 7 days"));
    const kv = el("div", "act-kv");
    const add = (k, v, cls) => kv.append(el("span", "k", k), el("span", cls || null, String(v)));
    add("created", cr.created);
    for (const [src, n] of Object.entries(cr.by_source || {})) add(src === "owner" ? "you" : src, n);
    add("created by a run", cr.by_run + " of " + cr.created, cr.by_run ? "warn" : "");
    add("new cards per run", cr.runs ? rate.toFixed(2) + " over " + cr.runs + " run" + (cr.runs === 1 ? "" : "s") : "no runs in the window", rate >= 0.5 ? "bad" : "");
    b.append(kv); view.append(b);
  }

  async function render() {
    await loadEvents();
    drawList();
    if (Helm.route.view === "activity" && !Helm.route.tid) drawPage();
  }

  Helm.on("route", (r) => {
    clearInterval(timer); timer = null;
    if (r.view !== "activity") return;
    render();
    timer = setInterval(() => { if (!document.hidden && Helm.route.view === "activity") render(); }, 60_000);
  });
  Helm.on("refresh", () => { if (Helm.route.view === "activity") render(); });
  Helm.on("acted", () => { if (Helm.route.view === "activity") render(); });
  // The count is wanted on every screen (the nav row), so today's events are read once on load;
  // the row's mark is too, and it is kept current every 60 s ().
  Helm.ready.then(() => {
    loadEvents();
    if (!Helm.has("activity")) return;
    pollHealth();
    setInterval(pollHealth, 60_000);
  });
})();
