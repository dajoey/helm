/* NEEDS YOU: the old inbox's lit cards, as the first section of the left pane.
   One row per thread waiting on the owner — NOW, then THIS WEEK, then WHENEVER, newest first
   (helm-web's needsYou()); tapping a row opens its conversation in the middle pane, where
   the ask's options sit in the composer and can be answered without scrolling at 999×1392.
   Answers first: when NEEDS YOU opens with nothing selected, the first ask opens on a wide
   screen (on a phone the list is the first pane of the stack). The header is the label bar and
   EMERGENCIES ONLY (see "the label bar" below). Data comes from app.js (`state`, the 60 s poll). */
"use strict";
(() => {
  const host = document.getElementById("list-needs-you");
  const lit = () => needsYou();

  // The designed empty state for the middle pane (Needs you, Threads, Activity, Search share it): a glyph in a ring, a
  // serif line, one plain sentence, and optionally one action. Sentence case; nothing in capitals.
  //   Helm.emptyState(title, sub, { kind: "ok"|"", glyph: "ask"|..., action: { label, href } })
  Helm.emptyState = (title, sub, opts) => {
    opts = opts || {};
    const v = document.getElementById("view-empty"); if (!v) return null;
    v.replaceChildren(Helm.emptyNode(title, sub, opts));
    return v;
  };
  Helm.emptyNode = (title, sub, opts) => {
    opts = opts || {};
    const e = el("div", "hm-empty empty-state " + (opts.kind || ""));
    const ring = el("span", "es-ring"); ring.append(Helm.glyph(opts.glyph || "thread", 26)); e.append(ring);
    e.append(el("span", "hm-empty-t", title));
    if (sub) e.append(el("span", "hm-empty-s", sub));
    if (opts.action) { const a = el("a", "btn secondary sm", opts.action.label); a.href = opts.action.href; e.append(a); }
    return e;
  };
  let opened = false;   // answers first fires once per visit, not on every poll

  // The one thread row (Needs you, Threads, Activity, Search draw the same shape; web/style.css "Helm rows"):
  // age in the time gutter (its clock time under it), a node on the rule, then title, ask line and a quiet meta line.
  function row(t, on) {
    const a = el("a", "lrow needs-you " + (t.urgency || "whenever") + (t.urgency === "now" ? " hot" : "") + (on ? " on" : ""));
    a.href = Helm.convHref(t.thread_id, "needs-you");
    a.dataset.id = t.thread_id;
    a.append(el("span", "bar"));
    const main = el("div");
    main.append(el("div", "ltitle", (t.catchup && t.catchup.subject) || t.title));
    const prompt = (t.ask && t.ask.prompt || "").trim();
    if (prompt) main.append(el("div", "lask", prompt));
    const meta = el("div", "lmeta");
    const u = t.urgency || "whenever";
    meta.append(el("span", "badge urg-" + u, u === "now" ? "Now" : u === "this-week" ? "This week" : "Whenever"));
    if (t.category) {
      // Plain text beside the urgency badge, not a second box (the owner, 2026-10-01). On a wide screen a click on it
      // filters by it; on a phone a tap there is a tap on the row, so it opens the card like every other tap.
      const c = t.category.trim(), cat = el("span", "lcat", c);
      cat.title = "Show only " + c;
      cat.onclick = (e) => { if (Helm.isPhone()) return; e.preventDefault(); e.stopPropagation(); toggle(c); };
      meta.append(cat);
    }
    if (state.gateIds.has(t.thread_id)) meta.append(el("span", "badge wait-badge", "gate"));
    main.append(meta);
    const age = el("span", "lage", fmtAge(askedAt(t)));
    age.title = "When this question was put to you";
    const at = askedAt(t);
    if (at) age.dataset.clock = new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    a.append(main, age);
    return a;
  }

  // ---------- the label bar ----------
  // It used to be one chip per label on any open thread: twenty labels filled a third of the pane before the
  // first card (the owner, 2026-10-01: "I don't want to reduce the amount of labels... is there a less intrusive way
  // to use them?"). Now: ALL, the PINS labels with the most waiting on him (each with its count), a LABELS chip
  // that opens a type-to-filter picker of every label, and FIRE ONLY. A label that is being filtered on is always
  // a chip (ending in a cross), so it can be taken off. With SHOW_ALL_AT labels or fewer there is nothing to
  // hide and every label is a chip. Every label stays reachable; only how it is reached changed.
  const PINS = 3, SHOW_ALL_AT = PINS + 2;
  let pickOpen = false, pickQuery = "", focusNext = "";   // the picker's state; focusNext: "pick" | "more" after the next draw
  const byName = (a, b) => a.localeCompare(b);
  const toggle = (c) => { state.categories.has(c) ? state.categories.delete(c) : state.categories.add(c); saveCats(); render(); };

  // label -> how many wait on him now. Every label on an open thread is a key (0 when none wait: the label filter
  // reaches the in-flight, on-hold and backlog lists too, so a label with nothing waiting still filters). The label
  // filter itself is left out, so the numbers and the pins don't move as he picks.
  function labelCounts() {
    const m = new Map();
    for (const t of state.threads) {
      const c = (t.category || "").trim();
      if (c && t.state !== "done" && !m.has(c)) m.set(c, 0);
    }
    for (const t of state.threads) {
      const c = (t.category || "").trim();
      if (!c || t.state !== "needs-you" || isSnoozed(t) || (state.emergenciesOnly && t.urgency !== "now")) continue;
      m.set(c, (m.get(c) || 0) + 1);
    }
    return m;
  }

  function labelChip(c, n) {
    const on = state.categories.has(c);
    const ch = el("button", "chip" + (on ? " on" : ""), c); ch.type = "button"; ch.dataset.label = c;
    ch.append(el("span", "chip-n", on ? "\u00d7" : String(n)));
    ch.title = on ? "Stop filtering by " + c : n + " waiting on you in " + c;
    ch.setAttribute("aria-label", on ? "Stop filtering by " + c : c + ", " + n + " waiting on you");
    ch.onclick = () => toggle(c);
    return ch;
  }

  function pick(c) { pickOpen = false; pickQuery = ""; toggle(c); }

  function picker(counts, cats) {
    const p = el("div", "lpick");
    const q = el("input"); q.type = "search"; q.placeholder = "find a label"; q.value = pickQuery; q.setAttribute("aria-label", "find a label");
    p.append(q);
    const fill = () => {
      while (q.nextSibling) q.nextSibling.remove();
      const needle = pickQuery.trim().toLowerCase();
      const rows = cats.filter(c => c.toLowerCase().includes(needle)).sort((a, b) => counts.get(b) - counts.get(a) || byName(a, b));
      if (!rows.length) { p.append(el("div", "lempty", "no label matches")); return; }
      let divided = false;
      for (const c of rows) {
        const n = counts.get(c), on = state.categories.has(c);
        if (!n && !divided) { divided = true; p.append(el("div", "lgroup", "nothing waiting")); }
        const r = el("button", "chip lpick-row" + (on ? " on" : "")); r.type = "button"; r.dataset.label = c; r.dataset.n = n;
        r.append(el("span", null, c), el("span", "chip-n", on ? "\u00d7" : String(n)));
        r.setAttribute("aria-label", on ? "Stop filtering by " + c : c + ", " + n + " waiting on you");
        r.onclick = () => pick(c);
        p.append(r);
      }
    };
    q.oninput = () => { pickQuery = q.value; fill(); };
    q.onkeydown = (e) => {
      const first = p.querySelector(".lpick-row");
      if (e.key === "Enter" && first) { e.preventDefault(); pick(first.dataset.label); }
    };
    fill();
    return p;
  }

  function header() {
    const h = el("div", "lhead");
    const counts = labelCounts();
    const cats = [...counts.keys()].sort(byName);
    if (cats.length <= SHOW_ALL_AT) pickOpen = false;   // nothing to open: it would pop back up when labels grow again
    if (cats.length > 1 || state.categories.size) {   // a filter in force always has its chips, even with one label left
      const all = el("button", "chip" + (state.categories.size ? "" : " on"), "All"); all.type = "button";
      all.onclick = () => { state.categories.clear(); saveCats(); render(); }; h.append(all);
      const hide = cats.length > SHOW_ALL_AT;
      const shown = new Set(hide ? cats.filter(c => counts.get(c) > 0).sort((a, b) => counts.get(b) - counts.get(a) || byName(a, b)).slice(0, PINS) : cats);
      for (const c of state.categories) if (counts.has(c)) shown.add(c);
      for (const c of [...shown].sort(byName)) h.append(labelChip(c, counts.get(c)));
      if (hide) {
        const more = el("button", "chip more" + (pickOpen ? " on" : ""), "Labels"); more.type = "button";
        more.title = "All " + cats.length + " labels, with how many are waiting on you";
        more.setAttribute("aria-expanded", pickOpen ? "true" : "false");
        more.onclick = () => { pickOpen = !pickOpen; pickQuery = ""; focusNext = pickOpen && !Helm.isPhone() ? "pick" : ""; draw(); };
        h.append(more);
      }
    }
    h.append(el("span", "grow"));
    const em = el("button", "chip" + (state.emergenciesOnly ? " on" : ""), "Fire only"); em.type = "button";
    em.title = "Show only the asks marked Now";
    em.onclick = () => { state.emergenciesOnly = !state.emergenciesOnly; try { localStorage.setItem("helm.emerg", state.emergenciesOnly ? "1" : "0"); } catch { /* convenience */ } render(); };
    h.append(em);
    if (pickOpen && cats.length > SHOW_ALL_AT) h.append(picker(counts, cats));
    return h;
  }

  // Escape closes the picker (and is not also the shell's Escape), with the focus back on the chip that opened it.
  host.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !pickOpen) return;
    e.preventDefault(); e.stopPropagation();
    pickOpen = false; pickQuery = ""; focusNext = "more"; draw();
  });

  function draw() {
    const r = Helm.route;
    const on = r.view === "needs-you" ? r.tid : null;
    const typing = !!(document.activeElement && document.activeElement.matches && document.activeElement.matches(".lpick input"));
    host.replaceChildren(titleBar(), header());
    const qi = host.querySelector(".lpick input");
    if (qi && (typing || focusNext === "pick")) { qi.focus(); qi.setSelectionRange(qi.value.length, qi.value.length); }
    else if (focusNext === "more") { const m = host.querySelector(".chip.more"); if (m) m.focus(); }
    focusNext = "";
    // Extra asks sit above the Helm cards: H.hooks.needsYouExtra, a no-op until something adds one.
    for (const n of Helm.hooks.needsYouExtra.run({ host, route: r })) host.append(n);
    const items = lit();
    if (state.loading && !state.threads.length) { host.append(el("div", "lempty", "Reading the board")); return; }
    if (state.offline && !state.threads.length) { host.append(el("div", "lempty", "Can't reach the gateway")); return; }
    if (!items.length) {
      host.append(el("div", "lempty ok", state.emergenciesOnly && emergencyCount() === 0 ? "Nothing is marked Now" : "All clear. Nothing needs you."));
    }
    for (const t of items) host.append(row(t, t.thread_id === on));
    // The middle pane when nothing is selected: the first ask opens (answers first), or all clear.
    if (r.view === "needs-you" && !r.tid) {
      if (items.length && !Helm.isPhone() && !opened) { opened = true; Helm.replace(Helm.convHref(items[0].thread_id, "needs-you")); return; }
      if (!items.length) Helm.emptyState(state.emergenciesOnly ? "Nothing is marked Now" : "All clear", youCount() ? `${youCount()} waiting, none urgent.` : "Nothing is waiting on you. Everything is in someone else's hands.", { kind: "ok", glyph: "check" });
      else Helm.emptyState("Pick an ask", "Choose one on the left to read it and answer it here.", { glyph: "ask" });
    }
  }

  // The list column's head: the section name in serif, then one quiet line of what is waiting.
  function titleBar() {
    const h = el("header", "list-title");
    const extra = typeof Helm.extraNeeds === "function" ? (Helm.extraNeeds() || 0) : 0;
    const n = items0().length + extra, hot = items0().filter(t => t.urgency === "now").length;
    h.append(el("h2", null, "Needs you"));
    h.append(el("p", "hm-meta", n ? `${n} ask${n === 1 ? "" : "s"}${hot ? `, ${hot} Now` : ""}` : "Nothing waiting"));
    return h;
  }
  const items0 = () => lit();
  // the extra asks change on their own clock: the title line follows them without a full redraw
  Helm.retitleNeeds = () => { const old = host.querySelector(".list-title"); if (old) old.replaceWith(titleBar()); };

  Helm.on("threads", draw);
  Helm.on("route", (r) => { pickOpen = false; pickQuery = ""; if (r.view !== "needs-you") opened = false; if (r.view === "needs-you") draw(); });
})();
