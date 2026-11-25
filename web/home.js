/* Today (#/home): the ship's log of the day and what needs you, in one page. Fills #view-home (the Today section the shell registers).

   Left: the greeting and date, one sentence of what is waiting and moving, the line box ("Write a line in today's log"), then the log:
   a time gutter, a glyph node per entry, a brass Now line, scheduled jobs dashed above it, today's entries below it, newest first.
   Right: Waiting on you (the first ask is answerable in place), Running now, Coming up, Recently touched.

   Entries come from what the app already reads, nothing here polls faster than a minute:
     /api/day?date=today        threads activity (filed, asked, answered ...), pages changed today, today's journal page
     /api/notes/changes?since=  who added which lines to which pages today (notebook module; the day's list is the fallback)
     Helm.on("threads")         the needs-you set (app.js)
   The line box POSTs {date, line} to /api/day/append, the notebook's own append, and shows only where the notebook is writable
   (Helm.notesMode is not "folder-readonly").

   An ask is answered through the conversation's own path: act(null, thread, "reply", { value }) (app.js), the buttons carry
   data-submit like every answer button. An option that needs a note, a confirm step or the draft sheet opens the card instead.
   A typed answer or a half-written log line is never lost to a redraw: the page waits for the field to lose focus.

   API (window.Helm.home):  refresh() read again now;  focusLine() focus the line box;  entries() the log as data (tests).   */
"use strict";
(() => {
  const H = window.Helm;
  if (!H) return;
  const root = document.getElementById("view-home");
  if (!root) return;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clip = (s, n) => { s = String(s == null ? "" : s).replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  const iso = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const nowS = () => Date.now() / 1000;
  const startOfDay = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
  const NUM = ["No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten"];
  const word = (n) => (n >= 0 && n < NUM.length ? NUM[n] : String(n));
  const inWords = (sec) => {
    if (sec < 45) return "now";
    if (sec < 3600) return "in " + Math.max(1, Math.round(sec / 60)) + " min";
    if (sec < 86400) { const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return "in " + h + " h" + (h < 6 && m >= 10 && m < 50 ? " " + m + " min" : ""); }
    return "in " + Math.round(sec / 86400) + " d";
  };
  const hasNb = () => H.has("notebook");
  const canWrite = () => hasNb() && H.notesMode !== "folder-readonly";

  // ---------- data ----------
  const D = { day: null, changes: null, activity: null, dayOk: false, loaded: false, error: "", seq: 0, showAll: false };
  const MORE_AFTER = 24;
  let onHome = false, lastAt = 0, pendingPaint = false, tick = 0;

  async function readDay() {
    const my = ++D.seq;
    const date = iso(new Date());
    const jobs = [api("/api/day?date=" + date).catch((e) => { D.error = e.message || "the day did not load"; return null; })];
    jobs.push(hasNb() ? api("/api/notes/changes?since=" + startOfDay() + "&limit=80").catch(() => null) : Promise.resolve(null));
    const [day, ch] = await Promise.all(jobs);
    if (my !== D.seq) return;
    if (day) { D.day = day; D.error = ""; }
    D.dayOk = !!day;
    D.changes = ch && Array.isArray(ch.changes) ? ch.changes : null;
    D.loaded = true; lastAt = Date.now();
    paint();
  }
  const srv = () => nowS() + (D.activity ? D.activity.skew : 0);

  // ---------- who ----------
  const ME = /^(you|owner|web)$/i;
  const whoName = (c) => { const p = String(c.principal || c.source || "").trim(); return !p || ME.test(p) ? "You" : p; };

  // ---------- the log ----------
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const STATUS_WORD = { running: "started", queued: "queued", waiting: "is waiting for you", done: "finished", failed: "failed", interrupted: "was interrupted", stopped: "was stopped", scheduled: "is scheduled" };
  const LINK_TYPE = { card: "task", session: "chat", chat: "chat", cron: "job" };
  function linkOf(i) {
    const l = i.link || {};
    if (typeof hx === "undefined") return "";
    switch (l.type) {
      case "card": return hx.link("card", l.id);
      case "session": return hx.link("session", l.id, l.profile);
      case "chat": return hx.link("chat", null, l.profile);
      case "cron": return hx.link("manage", "automations");
      default: return "";
    }
  }
  const chipOf = (i, fallbackType) => {
    const priv = i.private === true;
    const label = priv && !i.title ? "Private" : (i.title || i.what || "Item");
    return H.chip({ type: LINK_TYPE[(i.link || {}).type] || fallbackType || "run", id: (i.link || {}).id, label, href: linkOf(i) || undefined, state: STATUS_WORD[i.status] || "", private: priv && !!i.title });
  };
  // The text of a journal block: the last few non-empty lines, markdown marks dropped.
  const plain = (s) => String(s || "").replace(/^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|#{1,6}\s+)/, "").replace(/\*\*|__|`/g, "").replace(/\[\[([^\]|]+)\|?([^\]]*)\]\]/g, (m, a, b) => b || a).trim();
  const wholeLine = (s) => { const p = plain(s); return !!p && !/^\d{4}-\d{2}-\d{2}$/.test(p); };   // a page's date heading is not a line

  function dayLines() {
    const d = D.day;
    if (!d || !d.exists || !d.html) return [];
    try {
      const doc = new DOMParser().parseFromString(String(d.html), "text/html");
      return Array.from(doc.querySelectorAll("li, p")).filter((n) => !n.querySelector("li, p") && !/^h\d$/i.test(n.tagName)).map((n) => n.textContent.trim()).filter(Boolean);
    } catch { return []; }
  }
  const dayRel = () => (D.day && D.day.rel) || "";

  function noteChip(rel, label) {
    const day = /(?:^|\/)(\d{4}-\d{2}-\d{2})\.md$/.exec(rel);
    if (day) return H.chip({ type: "day", id: rel, label: label || day[1], href: H.dayHref(day[1]) });
    return H.chip({ type: "note", id: rel, label: label || rel.replace(/\.md$/i, "").split("/").pop(), href: H.noteHref(rel) });
  }

  function entries() {
    const out = [];
    const d = D.day;
    const rel = dayRel();
    const seenRels = new Set();
    // pages written to today, who and what (notebook changes), merged per page and writer inside 15 minutes
    if (D.changes) {
      const merged = [];
      for (const c of D.changes.slice().sort((a, b) => b.ts - a.ts)) {
        const who = whoName(c);
        const prev = merged.find((m) => m.rel === c.rel && m.who === who && Math.abs(m.ts - c.ts) < 900);
        const lines = (c.lines || []).filter(wholeLine).map(plain);
        if (prev) { prev.lines = lines.concat(prev.lines).slice(-3); continue; }
        merged.push({ rel: c.rel, who, ts: c.ts, title: c.title, lines: lines.slice(-3), kind: c.kind });
      }
      for (const m of merged) {
        seenRels.add(m.rel);
        const journal = m.rel === rel;
        out.push({ ts: m.ts, type: journal ? "day" : "note", glyph: journal ? "day" : "note", who: m.who, verb: (m.who === "You" ? "wrote in" : m.kind === "append" || !m.kind ? "appended to" : "changed"), chip: noteChip(m.rel, journal ? undefined : m.title), lines: m.lines, read: journal && m.who === "You", key: "note:" + m.rel + ":" + m.ts });
      }
    }
    // today's journal page, when the writes did not say (no notebook change feed): its last lines, stamped with the page's time
    if (d && d.exists && rel && !seenRels.has(rel)) {
      const lines = dayLines().filter(wholeLine).map(plain).filter((l) => l !== d.date).slice(-3);
      if (lines.length) { seenRels.add(rel); out.push({ ts: d.mtime || startOfDay(), type: "day", glyph: "day", who: "You", verb: "wrote in", chip: noteChip(rel), lines, read: true, key: "day:" + rel }); }
    }
    // other pages changed today
    for (const n of (d && d.changed) || []) {
      if (!n || !n.rel || seenRels.has(n.rel)) continue;
      seenRels.add(n.rel);
      out.push({ ts: n.mtime, type: "note", glyph: "note", who: "", verb: "Page changed", chip: noteChip(n.rel, n.title), lines: [], key: "chg:" + n.rel });
    }
    // threads: what was filed, asked, answered, closed
    const askedFor = new Set(((d && d.timeline) || []).filter((e) => e.kind === "asked").map((e) => e.thread_id));
    for (const e of (d && d.timeline) || []) {
      if (!e || !e.ts || !e.thread_id) continue;
      if (e.kind === "filed" && askedFor.has(e.thread_id)) continue;
      const lit = e.state === "needs-you";
      const asked = e.kind === "asked";
      const done = e.kind === "answered" || e.kind === "settled";
      const chip = H.chip({ type: asked ? "ask" : "thread", id: e.thread_id, label: e.title || e.thread_id, href: H.convHref(e.thread_id, lit ? "needs-you" : "threads"), state: lit ? "Waiting on you" : "" });
      const detail = e.kind === "asked" ? "" : clip(String(e.detail || "").replace(/^ASKED YOU \([^)]*\):\s*/, "").replace(/^\[[a-z]+\]\s*/, ""), 150);
      out.push({ ts: e.ts, type: asked ? "ask" : done ? "run" : "thread", glyph: done ? "check" : asked ? "ask" : "thread", who: "", verb: cap(String(e.label || e.kind || "")), chip, lines: detail && e.kind !== "filed" ? [detail] : [], quiet: !asked, key: "th:" + e.thread_id + ":" + e.kind + ":" + e.ts });
    }
    out.sort((a, b) => b.ts - a.ts);
    // pages changed in the same minute read as one line, not a row each
    const res = [];
    for (const e of out) {
      const prev = res[res.length - 1];
      if (e.verb === "Page changed" && prev && prev.verb === "Page changed" && Math.floor(prev.ts / 60) === Math.floor(e.ts / 60)) { prev.more = (prev.more || []).concat(e.chip); prev.n = (prev.n || 1) + 1; prev.verb = prev.n + " pages changed"; continue; }
      res.push(e);
    }
    return res;
  }
  // scheduled jobs for the part of the log above the Now line, latest at the top
  function upcomingJobs() {
    if (!D.activity) return [];
    const now = srv();
    const all = D.activity.upcoming.filter((i) => i && i.kind === "cron" && (i.next_at != null || i.at != null)).map((i) => ({ i, t: i.next_at != null ? i.next_at : i.at }))
      .filter((x) => x.t > now - 60).sort((a, b) => a.t - b.t);
    // The same job at the same minute under several profiles is one row that lists the profiles (the Private mark stays if any is private).
    const groups = [];
    for (const x of all) {
      const base = String(x.i.title || "").replace(/\s*\([^()]*\)\s*$/, "").trim();
      const key = base.toLowerCase() + "|" + Math.floor(x.t / 60);
      const g = base ? groups.find((y) => y.key === key) : null;
      if (g) { g.profiles.push(x.i.profile || ""); if (x.i.private === true) g.i = Object.assign({}, g.i, { private: true }); }
      else groups.push({ key, i: x.i, t: x.t, base, profiles: [x.i.profile || ""] });
    }
    for (const g of groups) if (g.profiles.length > 1) g.i = Object.assign({}, g.i, { title: g.base });
    return groups.slice(0, 3).reverse();
  }
  function comingUp() {
    const now = srv(), out = [];
    for (const x of upcomingJobs()) out.push({ t: x.t, kind: "job", i: x.i });
    for (const t of state.threads || []) {
      const at = t.state !== "needs-you" ? (t.wake_at && t.wake_at * 1000 > Date.now() ? t.wake_at : (t.recheck_at && t.recheck_at > now ? t.recheck_at : 0)) : 0;
      if (at && t.state !== "done") out.push({ t: at, kind: "thread", th: t });
    }
    return out.sort((a, b) => a.t - b.t).slice(0, 6);
  }
  H.home = { entries, refresh: () => { readDay(); }, focusLine: () => { const f = root.querySelector(".hm-home-in"); if (f) f.focus(); }, state: D };

  // ---------- drawing: the log ----------
  function entryNode(e) {
    const li = el("li", "hm-log-entry t-" + e.type + (e.fut ? " fut" : "") + (e.bad ? " is-bad" : ""));
    const t = el("div", "hm-log-t", clock(e.ts));
    if (e.fut) t.append(el("small", null, inWords(e.ts - srv())));
    const nd = el("div", "hm-log-nd"); const dot = el("i"); dot.append(H.glyph(e.glyph || e.type)); nd.append(dot);
    const bd = el("div", "hm-log-bd");
    const p = el("p", "hm-log-line" + (e.quiet ? " quiet" : ""));
    if (e.who) p.append(el("b", "hm-log-who", e.who), " ");
    p.append(e.verb ? e.verb + " " : "", e.chip);
    for (const c of e.more || []) p.append(" ", c);
    bd.append(p);
    for (const l of e.lines || []) bd.append(el("p", e.read ? "hm-log-read" : "hm-log-text", l));
    if (e.run && typeof H.pulse !== "undefined" && H.pulse && H.pulse.segments) {
      const seg = H.pulse.segments().find((s) => s.kind === "run" && s.id === (e.run.link || {}).id);
      if (seg && seg.pct != null) { const m = el("div", "hm-meter"); const b = el("i"); b.style.width = Math.max(2, seg.pct) + "%"; m.append(b); bd.append(m); }
    }
    li.append(t, nd, bd);
    return li;
  }
  function nowNode() {
    const n = el("li", "hm-log-now"); n.id = "home-now";
    n.append(el("span", "hm-log-t", clock(nowS())), el("i"), el("b"));
    n.setAttribute("aria-label", "Now");
    return n;
  }
  function logNode() {
    const wrap = el("div", "hm-home-logwrap");
    const ol = el("ol", "hm-log hm-home-log"); ol.id = "home-log";
    for (const x of upcomingJobs()) {
      ol.append(entryNode({ ts: x.t, type: "job", glyph: "job", fut: true, who: "", verb: "Scheduled", chip: chipOf(x.i, "job"), lines: x.profiles.length > 1 ? [x.profiles.filter(Boolean).join(", ")] : [], quiet: true }));
    }
    ol.append(nowNode());
    const list = entries();
    const shown = D.showAll ? list : list.slice(0, MORE_AFTER);
    for (const e of shown) ol.append(entryNode(e));
    wrap.append(ol);
    if (!D.loaded) wrap.append(el("div", "hm-empty-s", "Reading today…"));
    else if (!list.length) {
      const em = el("div", "hm-empty hm-home-empty");
      em.append(el("div", "hm-empty-t", "Nothing has happened today yet."), el("div", "hm-empty-s", canWrite() ? "Write the first line above, or wait for the day to fill in." : "Asks, runs and page changes appear here as they happen."));
      wrap.append(em);
    }
    if (list.length > shown.length) {
      const b = el("button", "btn quiet hm-home-more", "Show " + (list.length - shown.length) + " earlier"); b.type = "button";
      b.onclick = () => { D.showAll = true; paint(); };
      wrap.append(b);
    }
    return wrap;
  }

  // ---------- drawing: the head ----------
  function greeting() {
    const h = new Date().getHours();
    const g = h >= 5 && h < 12 ? "Good morning" : h >= 12 && h < 17 ? "Good afternoon" : h >= 17 && h < 22 ? "Good evening" : "Late night";
    const name = H.meta && (H.meta.user || H.meta.name);
    return g + (name ? ", " + name : "") + ".";
  }
  const needCount = () => (typeof H.count === "function" ? H.count("needs-you") : 0) || 0;
  function summaryNode() {
    const p = el("p", "hm-home-sum hm-lede");
    const em = (s) => el("b", "hm-home-em", s);
    const n = needCount();
    if (n > 0) p.append(em(word(n) + (n === 1 ? " thing" : " things")), n === 1 ? " is waiting on you" : " are waiting on you");
    else p.append("Nothing is waiting on you");
    p.append(". Here is the log for today.");
    return p;
  }
  // the line box: appends to today's journal through the notebook's own route
  let lineDraft = "";
  function lineBox() {
    const f = el("form", "hm-home-lineform"); f.id = "home-line"; f.autocomplete = "off";
    const gl = H.glyph("pen"); gl.classList.add("hm-home-pen");
    const inp = el("input", "hm-input hm-home-in"); inp.type = "text"; inp.maxLength = 4000; inp.placeholder = "Write a line in today's log"; inp.setAttribute("aria-label", "Write a line in today's log");
    inp.value = lineDraft;
    inp.oninput = () => { lineDraft = inp.value; go.disabled = !inp.value.trim(); };
    const go = el("button", "btn primary hm-home-add", "Add line"); go.disabled = !inp.value.trim();
    f.append(gl, inp, go);
    const fire = async () => {
      const line = inp.value.trim();
      if (!line || f.classList.contains("busy")) return;
      f.classList.add("busy"); go.disabled = true;
      try {
        await api("/api/day/append", { method: "POST", body: JSON.stringify({ date: iso(new Date()), line }) });
        lineDraft = ""; inp.value = "";
        toast("Added to today's log");
        await readDay();
      } catch (e) { toast("That line did not go in: " + (e && e.message || "try again"), true); }   // the text stays in the box
      finally { f.classList.remove("busy"); go.disabled = !inp.value.trim(); }
    };
    markSubmit(go, fire);   // the v10 submit rule: a press and release on the button itself, or Enter in the box
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); if (!go.disabled && go._act) go._act(); } };
    f.onsubmit = (ev) => { ev.preventDefault(); };
    return f;
  }

  // ---------- drawing: the side column ----------
  const secHead = (title, extra) => {
    const h = el("div", "hm-home-sh"); h.append(el("h2", "hm-home-st", title));
    if (extra) h.append(extra);
    return h;
  };
  const askableOption = (o) => !!o && o.value !== "amend" && !/^\s*__use_draft__/i.test(String(o.value || ""));
  const answers = new Map();   // thread id -> the text typed into an in-place answer
  function askCard(t) {
    const ask = t.ask || {};
    const card = el("article", "hm-card ask hm-home-ask"); card.dataset.id = t.thread_id;
    const meta = el("div", "hm-home-ask-meta");
    if (t.category) meta.append(el("span", "hm-pill soft", t.category));
    if (t.urgency === "now") meta.append(el("span", "hm-pill now", "Now"));
    if (meta.childNodes.length) card.append(meta);
    const h = el("h3", "hm-home-ask-t"); const a = el("a", null, t.title); a.href = H.convHref(t.thread_id, "needs-you"); h.append(a);
    card.append(h);
    const blurb = clip(ask.prompt && ask.prompt !== t.title ? ask.prompt : (t.summary || ""), 240);
    if (blurb) { const p = el("p", "hm-home-ask-s"); appendRefText(blurb, p); card.append(p); }
    const reply = (body, msg) => act(card, t, "reply", body, msg);
    const opts = ask.options || [];
    const list = ask.kind === "choice" || opts.length > 1 ? opts : ask.kind === "ack" ? [{ label: (opts[0] && opts[0].label) || "Got it", value: (opts[0] && opts[0].value) || "ack", effect: (opts[0] && opts[0].effect) || "" }] : [];
    const inPlace = ask.kind === "text" || (list.length && list.every(askableOption));
    if (ask.kind === "text") {
      const f = el("div", "hm-home-send");
      const inp = el("input", "hm-input"); inp.type = "text"; inp.maxLength = 4000; inp.placeholder = "Your answer"; inp.setAttribute("aria-label", "Your answer"); inp.value = answers.get(t.thread_id) || "";
      const go = el("button", "btn primary", "Send"); go.disabled = !inp.value.trim(); go.title = "Send this answer";
      const fire = () => { const v = inp.value.trim(); if (v) reply({ value: v }, "Sent").then((r) => { if (r) answers.delete(t.thread_id); }); };
      markSubmit(go, fire);
      inp.oninput = () => { answers.set(t.thread_id, inp.value); go.disabled = !inp.value.trim(); };
      inp.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); if (!go.disabled && go._act) go._act(); } };
      f.append(inp, go); card.append(f);
    } else if (inPlace) {
      const box = el("div", "hm-home-opts");
      list.forEach((o, i) => {
        const b = el("button", "hm-home-opt"); b.dataset.value = o.value;
        const rec = o.recommended === true || /\(recommended\)\s*$/i.test(String(o.label || ""));
        const lab = el("span", "hm-home-opt-l", String(o.label || o.value).replace(/\s*\(recommended\)\s*$/i, ""));
        if (rec) lab.append(" ", el("span", "hm-pill now hm-home-rec", "Recommended"));
        b.append(el("i", "hm-home-opt-r"), el("span", "hm-home-opt-b")); b.lastChild.append(lab);
        if (o.effect) b.lastChild.append(el("span", "hm-home-opt-e", o.effect));
        markSubmit(b, () => reply({ value: o.value, note: "" }, "Replied: " + (o.label || o.value)));
        box.append(b);
      });
      card.append(box);
    } else {
      const open = el("a", "btn primary hm-home-open", "Open the card"); open.href = H.convHref(t.thread_id, "needs-you");
      card.append(open);
    }
    const foot = el("div", "hm-home-ask-foot");
    const age = askedAt(t); if (age) foot.append(el("span", "hm-meta", "Asked " + fmtAge(age)));
    if (inPlace) { const o = el("a", "hm-home-link", "Open the card"); o.href = H.convHref(t.thread_id, "needs-you"); foot.append(o); }
    card.append(foot);
    return card;
  }
  function askRow(t) {
    const a = el("a", "hm-row hm-home-row t-ask"); a.href = H.convHref(t.thread_id, "needs-you"); a.dataset.id = t.thread_id;
    const gl = el("span", "hm-row-gl"); gl.append(H.glyph("ask"));
    const main = el("span", "hm-row-main"); main.append(el("span", "hm-row-t", t.title), el("span", "hm-row-s", [t.category, t.urgency === "now" ? "now" : (t.urgency && t.urgency !== "whenever" ? t.urgency.replace(/-/g, " ") : "")].filter(Boolean).join(", ")));
    a.append(gl, main, el("span", "hm-row-end", fmtAge(askedAt(t))));
    return a;
  }
  function waitingNode() {
    const sec = el("section", "hm-home-sec hm-home-wait"); sec.id = "home-wait";
    const asks = typeof needsYou === "function" ? needsYou() : [];
    const n = needCount();
    sec.append(secHead("Waiting on you", el("span", "hm-meta hm-home-count", n + (n === 1 ? " ask" : " asks"))));
    if (!asks.length && !n) { sec.append(el("p", "hm-home-none", "Nothing is waiting on you.")); return sec; }
    if (asks[0]) sec.append(askCard(asks[0]));
    const rest = asks.slice(1, 6);
    if (rest.length) { const l = el("div", "hm-list"); for (const t of rest) l.append(askRow(t)); sec.append(l); }
    const extra = n - asks.length;
    if (extra > 0) { const a = el("a", "hm-home-link hm-home-more-link", extra + (extra === 1 ? " more question" : " more questions") + " from chats, in Needs you"); a.href = "#/needs-you"; sec.append(a); }
    else if (asks.length > 6) { const a = el("a", "hm-home-link hm-home-more-link", "See all " + asks.length); a.href = "#/needs-you"; sec.append(a); }
    return sec;
  }
  function comingNode() {
    const sec = el("section", "hm-home-sec hm-home-soon"); sec.id = "home-soon";
    sec.append(secHead("Coming up"));
    const list = comingUp();
    if (!list.length) { sec.append(el("p", "hm-home-none", "Nothing scheduled.")); return sec; }
    const l = el("div", "hm-list");
    for (const c of list) {
      const a = el("a", "hm-row hm-home-row t-" + (c.kind === "job" ? "job" : "thread"));
      const gl = el("span", "hm-row-gl"); gl.append(H.glyph(c.kind === "job" ? "job" : "thread"));
      let title, href, sub = "";
      if (c.kind === "job") { const priv = c.i.private === true; title = priv && !c.i.title ? "Private job" : c.i.title || "Scheduled job"; href = "#/manage/automations"; sub = priv ? "Private" : ""; }
      else { title = c.th.title; href = H.convHref(c.th.thread_id, "threads"); sub = "Comes back"; }
      a.href = href;
      const main = el("span", "hm-row-main"); main.append(el("span", "hm-row-t", title)); if (sub) main.append(el("span", "hm-row-s", sub));
      const t = el("span", "hm-row-end"); t.append(clock(c.t), el("small", null, inWords(c.t - srv())));
      a.append(gl, main, t); l.append(a);
    }
    sec.append(l);
    return sec;
  }
  function touchedNode() {
    if (!hasNb()) return null;
    const sec = el("section", "hm-home-sec hm-home-touched"); sec.id = "home-touched";
    const open = el("a", "hm-home-link", "Open the notebook"); open.href = "#/notebook";
    sec.append(secHead("Recently touched", open));
    const rows = [], seen = new Set();
    for (const c of D.changes || []) { if (seen.has(c.rel)) continue; seen.add(c.rel); rows.push({ rel: c.rel, title: c.title, ts: c.ts }); }
    for (const n of ((D.day && D.day.changed) || []).slice().sort((a, b) => b.mtime - a.mtime)) { if (seen.has(n.rel)) continue; seen.add(n.rel); rows.push({ rel: n.rel, title: n.title, ts: n.mtime }); }
    if (D.day && D.day.exists && D.day.rel && !seen.has(D.day.rel)) rows.push({ rel: D.day.rel, title: D.day.date, ts: D.day.mtime || startOfDay() });
    rows.sort((a, b) => b.ts - a.ts);
    if (!rows.length) { sec.append(el("p", "hm-home-none", D.loaded ? "No page has changed today." : "Reading…")); return sec; }
    const l = el("div", "hm-list");
    for (const r of rows.slice(0, 5)) {
      const row = el("div", "hm-row hm-home-row");
      const gl = el("span", "hm-row-gl"); gl.append(H.glyph(/(?:^|\/)\d{4}-\d{2}-\d{2}\.md$/.test(r.rel) ? "day" : "note"));
      const main = el("span", "hm-row-main"); main.append(noteChip(r.rel, r.title));
      row.append(gl, main, el("span", "hm-row-end", clock(r.ts)));
      l.append(row);
    }
    sec.append(l);
    return sec;
  }

  // ---------- paint ----------
  function paint() {
    if (!onHome) return;
    // never take a field out from under him: a typed answer or line waits for the next quiet moment
    const ae = document.activeElement;
    const typing = ae && root.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA") && root.querySelector(".hm-home");
    // the palette hands the focus back to the field it came from, so a redraw waits for it to close too
    if (typing || document.body.classList.contains("pal-open")) { pendingPaint = true; refreshLive(); return; }
    pendingPaint = false;
    const page = el("div", "view hm-home");
    const head = el("header", "hm-home-head");
    head.append(el("p", "hm-home-date", new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }) + ", " + clock(nowS())));
    head.lastChild.id = "home-date";
    head.append(el("h1", "hm-home-hi hm-h1", greeting()), summaryNode());
    if (canWrite()) head.append(lineBox());
    page.append(head);
    if (D.error && !D.day) { const b = el("div", "hm-empty hm-home-err"); b.append(el("div", "hm-empty-t", "Today did not load."), el("div", "hm-empty-s", D.error)); page.append(b); }
    const side = el("aside", "hm-home-side"); side.setAttribute("aria-label", "What needs you");
    side.append(waitingNode());
    const rest = el("div", "hm-home-rest");
    for (const n of [comingNode(), touchedNode()]) if (n) rest.append(n);
    side.append(rest);
    const log = el("main", "hm-home-main"); log.append(logNode());
    page.append(side, log);
    const keep = root.firstElementChild ? root.firstElementChild.scrollTop : 0;
    root.replaceChildren(page);
    page.scrollTop = keep;
  }
  // the clocks and the Now line are kept true without redrawing the page under a typing hand
  function refreshLive() {
    const n = $("#home-now .hm-log-t"); if (n) n.textContent = clock(nowS());
    const d = $("#home-date"); if (d) d.textContent = new Date().toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" }) + ", " + clock(nowS());
  }
  const $ = (s, r = root) => r.querySelector(s);
  H.on("palette-close", () => { if (pendingPaint) setTimeout(paint, 0); });
  root.addEventListener("focusout", () => { if (pendingPaint) setTimeout(() => { if (!root.contains(document.activeElement) || !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) paint(); }, 0); });

  // ---------- life ----------
  H.on("threads", () => { if (onHome) paint(); });
  H.on("acted", () => { if (onHome) readDay(); });
  H.on("refresh", () => { if (onHome) { D.activity = null; readDay(); } });
  H.on("route", (r) => {
    const was = onHome;
    onHome = r.view === "home";
    if (!onHome) { clearInterval(tick); tick = 0; return; }
    if (!was) D.showAll = false;
    paint();
    readDay();
    if (!tick) tick = setInterval(() => {
      if (document.hidden || !onHome) return;
      if (Date.now() - lastAt > 60000) { readDay(); } else { refreshLive(); }
    }, 20000);
  });
  window.addEventListener("hashchange", () => { /* the route event does the work */ });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && onHome && Date.now() - lastAt > 60000) { readDay(); } });
  H.ready.then(() => { if (H.route && H.route.view === "home") { onHome = true; paint(); } });
})();
