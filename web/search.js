/* Search: one box over threads and notes, results grouped, in the middle pane. /api/search (the notebook's route) answers notes +
   threads together; where the notebook is off, or that route is missing, the read API's
   /api/triage/search carries the threads alone. A thread hit is a row (the same row the
   lists draw) that opens the conversation under THREADS; a note hit opens the note.      */
"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const root = $("#view-search");
  let seq = 0;

  // FTS snippets arrive with \x02 and \x03 around the matched words.
  // A snippet is a piece of a note's markdown; set in a reading face it should read as words, not syntax.
  const plain = (s) => String(s || "")
    .replace(/\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?/g, " ")          // a table's rule row
    .replace(/\[\[([^\]|]*)\|([^\]]*)\]\]/g, "$2").replace(/\[\[([^\]]*)\]\]/g, "$1")   // [[page|label]] and [[page]]
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")                               // links and images
    .replace(/(^|\s)#{1,6}\s+/g, "$1").replace(/(^|\s)>\s+/g, "$1")           // headings and quotes
    .replace(/(\*\*|__|~~|`)/g, "").replace(/(^|\s)[-*]\s+/g, "$1")
    .replace(/\s*\|\s*/g, " · ").replace(/\s+/g, " ").replace(/(\s·){2,}/g, " ·").replace(/^\s*·\s*/, "").replace(/\s*·$/, "").trim();
  function snippet(s) {
    const frag = document.createDocumentFragment();
    let mark = false;
    for (const part of plain(s).split(/([\u0002\u0003])/)) {
      if (part === "\u0002") { mark = true; continue; }
      if (part === "\u0003") { mark = false; continue; }
      if (!part) continue;
      frag.append(mark ? el("mark", null, part) : document.createTextNode(part));
    }
    return frag;
  }

  function form(q) {
    const f = el("form", "search-form");
    const inp = el("input"); inp.type = "search"; inp.placeholder = "Search threads and notes"; inp.value = q || ""; inp.setAttribute("aria-label", "Search");
    const go = el("button", "btn primary", "Search"); go.type = "submit";
    f.append(inp, go);
    f.onsubmit = (e) => { e.preventDefault(); const v = inp.value.trim(); if (v) Helm.go(Helm.searchHref(v)); };
    return f;
  }

  const stateLabel = (s) => ({ "needs-you": "needs you", working: "in flight", "waiting-external": "on hold", backlog: "backlog", done: "done" })[s] || s;
  function threadRow(t) {
    const a = el("a", "lrow " + t.state + (t.state === "needs-you" && t.urgency === "now" ? " hot" : ""));
    a.href = Helm.convHref(t.thread_id, t.state === "needs-you" ? "needs-you" : "threads");
    a.dataset.id = t.thread_id;
    a.append(el("span", "bar"));
    const main = el("div");
    main.append(el("div", "ltitle", t.title));
    const meta = el("div", "lmeta");
    meta.append(el("span", "badge " + (t.state === "needs-you" ? "urg-" + (t.urgency || "whenever") : t.state === "done" ? "done-badge" : t.state === "working" ? "agent-badge" : "wait-badge"), stateLabel(t.state)));
    if (t.category) meta.append(el("span", "lcat", t.category));   // quiet text, not a second box (2026-10-01)
    main.append(meta);
    if (t.summary) main.append(el("div", "lask", String(t.summary).slice(0, 160)));
    const age = el("span", "lage", fmtAge(askedAt(t)));
    const at = askedAt(t);
    if (at) age.dataset.clock = new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    a.append(main, age);
    return a;
  }

  async function run(q) {
    const my = ++seq;
    const view = el("div", "view sres");
    view.append(form(q));
    root.replaceChildren(view);
    Helm.setProps("Search", null);
    if (!q) { view.append(Helm.emptyNode("Search everything", "Type in the box at the top, or press / from anywhere. Notes and threads are searched together.", { glyph: "search" })); return; }
    view.append(el("div", "sres-wait", "Searching…"));
    let notes = null, threads = [], err = "";
    try {
      if (Helm.has("notebook")) {
        try {
          const r = await api("/api/search?q=" + encodeURIComponent(q));
          notes = r.notes || []; threads = r.threads || [];
        } catch (e) {
          if (!/^404/.test(e.message)) throw e;   // the notebook has not landed: threads only
        }
      }
      if (notes === null) {
        const r = await api("/api/triage/search?q=" + encodeURIComponent(q) + "&limit=40");
        threads = r.threads || [];
      }
    } catch (e) { err = e.message; }
    if (my !== seq) return;
    view.lastChild.remove();
    if (err) { view.append(Helm.emptyNode("Search did not work", err, { kind: "bad", glyph: "search" })); return; }
    if (notes !== null) {
      view.append(el("h3", null, `Notes · ${notes.length}`));
      if (!notes.length) view.append(el("div", "muted", "No note matches."));
      for (const n of notes) {
        const hit = el("div", "hit");
        const lbl = n.title || n.stem || n.rel;
        const isDay = !!n.daily || /^daily\//i.test(n.rel || "") || /^\d{4}-\d{2}-\d{2}$/.test(n.stem || "");
        const a = typeof Helm.chip === "function" ? Helm.chip({ type: isDay ? "day" : "note", id: n.rel, label: lbl, href: Helm.noteHref(n.rel) }) : Object.assign(el("a", null, lbl), { href: Helm.noteHref(n.rel) });
        hit.append(a, el("span", "where", n.folder || ""));
        if (isDay) { const d = el("a", "where", "As a day"); d.href = Helm.dayHref(n.stem); hit.append(" ", d); }
        if (n.snippet) { const s = el("div", "snip"); s.append(snippet(n.snippet)); hit.append(s); }
        view.append(hit);
      }
    }
    view.append(el("h3", null, `Threads · ${threads.length}`));
    if (!threads.length) view.append(el("div", "muted", "No thread matches."));
    else {
      const box = el("div", "sec-rows");
      for (const t of threads) box.append(threadRow(t));
      view.append(box);
      const all = el("a", "act-links", "See every thread matching this, with filters"); all.href = Helm.threadsHref({ q });
      view.append(all);
    }
  }

  Helm.on("route", (r) => { if (r.view === "search") run(r.q || ""); });
})();
