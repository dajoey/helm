/* The command palette: one box to jump anywhere or do anything. Ctrl/Cmd-K and "/" open it, the top search box opens it on a click,
   Esc closes it. Up and Down move, Enter opens, a click opens. The old #/search page and Ctrl+O's switcher are untouched: the
   palette's last row, "Search everything for ...", is the search page, and Enter with nothing picked goes there too.

   Groups (a group with nothing to show is not drawn):
     Actions                   file a card, write in today's log, today's page, refresh, answers first, the keys sheet
     Sections                  the rail's sections
     Notes and threads         /api/switcher (titles), read as you type

   Nothing is stored. A private card or session is never in the list, whatever it is called.

   API (window.Helm.palette):  open(query?)  close()  isOpen()  items()  the entries as drawn, for tests.                     */
"use strict";
(() => {
  const H = window.Helm;
  if (!H || !H.glyph) return;
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clip = (s, n) => { s = String(s == null ? "" : s).replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  const hit = (hay, q) => !q || String(hay || "").toLowerCase().includes(q);
  const PER = { empty: 4, typed: 6 };

  // ---------- state ----------
  const S = { open: false, q: "", sel: 0, rows: [], seq: 0, back: null, notes: [], threads: [], t: 0 };

  // ---------- the shell of the box ----------
  const root = el("div", "hm-pal"); root.id = "hm-pal"; root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true"); root.setAttribute("aria-label", "Search and jump");
  const box = el("div", "hm-pal-box");
  const input = el("input", "hm-pal-in"); input.id = "pal-q"; input.type = "text"; input.autocomplete = "off"; input.spellcheck = false;
  input.placeholder = "Jump to a note, thread or card";
  input.setAttribute("role", "combobox"); input.setAttribute("aria-expanded", "true"); input.setAttribute("aria-controls", "pal-list"); input.setAttribute("aria-label", "Search and jump");
  const list = el("ul", "hm-pal-list"); list.id = "pal-list"; list.setAttribute("role", "listbox");
  const foot = el("div", "hm-pal-foot");
  for (const [k, t] of [["Up Down", "move"], ["Enter", "open"], ["Esc", "close"]]) { const s = el("span"); s.append(el("kbd", "hm-kbd", k), " " + t); foot.append(s); }
  const close = el("button", "hm-pal-close"); close.type = "button"; close.append(el("span", null, "Search and jump"), el("span", null, "Close"));
  box.append(input, list, foot, close);   // on a phone the box is column-reverse: the input sits at the bottom, Close on top
  root.append(box);
  document.body.append(root);

  // ---------- what can be offered ----------
  function actions(q) {
    const out = [];
    const add = (name, glyph, key, run, prev) => { if (hit(name + " " + (prev || ""), q)) out.push({ group: "Actions", type: "action", glyph, name, key, run, prev }); };
    add("File a card", "plus", "N", () => { if (typeof openSheet === "function") openSheet(); }, "A new card for an agent" + (H.site === "work" ? ", or for someone you pick" : ""));
    if (H.has("notebook")) {
      if (H.notesMode !== "folder-readonly") add("Write in today's log", "pen", "", () => { H.go("#/home"); setTimeout(() => { if (H.home) H.home.focusLine(); }, 350); }, "Add a line to today's journal page");
      add("Open today's page", "day", "Ctrl D", () => H.go(H.dayHref(localISO())), "The journal page for today");
    }
    add("Refresh", "activity", "R", () => H.refresh(), "Read everything again");
    add("Answers first", "ask", "F", () => H.answersFirst(), "Jump to the first ask waiting on you");
    add("Keyboard shortcuts", "more", "?", () => H.openHelp(), "Every key the app knows");
    return out;
  }
  const localISO = () => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
  const GL = { home: "home", "needs-you": "needs", notebook: "book", threads: "threads", activity: "activity" };
  function sections(q) {
    const out = [];
    for (const s of H.sections()) {
      if (!hit(s.label + " " + (s.title || ""), q)) continue;
      out.push({ group: "Sections", type: "section", glyph: GL[s.key] || "threads", name: s.label, prev: s.title, href: "#/" + s.key });
    }
    return out;
  }
  const nodeRows = (q) => {
    const out = [];
    for (const n of S.notes) out.push({ group: "Notes and threads", type: n.daily ? "day" : "note", glyph: n.daily ? "day" : "note", name: n.title || n.stem || n.rel, prev: n.folder || "", href: H.noteHref(n.rel) });
    for (const t of S.threads) out.push({ group: "Notes and threads", type: t.state === "needs-you" ? "ask" : "thread", glyph: t.state === "needs-you" ? "ask" : "thread", name: t.title, prev: [t.state === "needs-you" ? "Waiting on you" : "", t.category].filter(Boolean).join(", "), href: H.convHref(t.thread_id, t.state === "needs-you" ? "needs-you" : "threads") });
    return out;
  };

  // ---------- build and draw ----------
  function build() {
    const q = S.q.trim().toLowerCase();
    const per = q ? PER.typed : PER.empty;
    const groups = [];
    const push = (rows, cap) => { if (rows.length) groups.push(rows.slice(0, cap || per)); };
    push(actions(q), 8);
    push(nodeRows(q), q ? 10 : 6);
    push(sections(q), q ? 8 : 12);
    const rows = [].concat(...groups);
    if (S.q.trim()) rows.push({ group: "Search", type: "search", glyph: "search", name: "Search everything for “" + clip(S.q.trim(), 60) + "”", prev: "Notes and threads, with filters", href: H.searchHref(S.q.trim()) });
    S.rows = rows;
    if (S.sel >= rows.length) S.sel = Math.max(0, rows.length - 1);
  }
  function draw() {
    list.replaceChildren();
    let g = "";
    S.rows.forEach((r, i) => {
      if (r.group !== g) { g = r.group; const h = el("li", "hm-pal-group", g); h.setAttribute("role", "presentation"); list.append(h); }
      const li = el("li"); li.setAttribute("role", "option"); li.id = "pal-opt-" + i; li.setAttribute("aria-selected", i === S.sel ? "true" : "false");
      const a = el(r.href ? "a" : "div", "hm-pal-item t-" + (r.type === "action" || r.type === "section" || r.type === "manage" || r.type === "search" ? "host" : r.type) + (i === S.sel ? " sel" : ""));
      if (r.href) a.setAttribute("href", r.href);
      a.dataset.i = String(i); a.tabIndex = -1;
      const gl = el("span", "hm-pal-gl"); gl.append(H.glyph(r.glyph));
      const name = el("span", "hm-pal-name"); name.append(el("span", null, r.name)); if (r.prev) name.append(el("span", "hm-pal-prev", clip(r.prev, 120)));
      a.append(gl, name);
      if (r.key) a.append(el("span", "hm-pal-key", r.key));
      li.append(a); list.append(li);
    });
    if (!S.rows.length) list.append(el("li", "hm-pal-group", "Nothing matches"));
    input.setAttribute("aria-activedescendant", S.rows.length ? "pal-opt-" + S.sel : "");
    const cur = list.querySelector(".sel"); if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
  }
  function refresh() { build(); draw(); }

  // notes and threads from the switcher, read as he types (the last answer wins)
  async function readSwitcher() {
    const my = ++S.seq;
    const q = S.q.trim();
    if (!H.has("notebook")) { S.notes = []; S.threads = []; refresh(); return; }
    try {
      const j = await api("/api/switcher?q=" + encodeURIComponent(q));
      if (my !== S.seq) return;
      S.notes = (j.notes || []).filter((n) => n && n.rel).slice(0, 10);
      S.threads = (j.threads || []).slice(0, 8);
    } catch { if (my !== S.seq) return; S.notes = []; S.threads = []; }
    refresh();
  }

  // ---------- open, close, choose ----------
  function openPal(q) {
    if (S.open) { if (q != null) { input.value = q; S.q = q; S.sel = 0; refresh(); readSwitcher(); } input.focus(); return; }
    S.open = true; S.q = q || ""; S.sel = 0; S.notes = []; S.threads = []; S.back = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    if (H.peek) H.peek.hide();
    if (H.pulse && H.pulse.closeSheet) H.pulse.closeSheet();
    input.value = S.q;
    root.classList.add("on"); document.body.classList.add("pal-open");
    refresh(); readSwitcher();
    input.focus(); input.select(); fitBox();
  }
  // On a phone the on-screen keyboard must not cover the input that sits at the bottom of the box.
  const fitBox = () => { const v = window.visualViewport; if (!v || !S.open || !window.matchMedia("(max-width: 760px)").matches) { box.style.height = ""; return; } box.style.height = Math.round(v.height) + "px"; };
  if (window.visualViewport) { window.visualViewport.addEventListener("resize", fitBox); window.visualViewport.addEventListener("scroll", fitBox); }
  function closePal(restore) {
    if (!S.open) return;
    S.open = false; clearTimeout(S.t); S.seq++;
    root.classList.remove("on"); document.body.classList.remove("pal-open"); box.style.height = "";
    const back = S.back; S.back = null;
    if (restore !== false && back && back.isConnected && back.focus) { try { back.focus({ preventScroll: true }); } catch { /* gone */ } }
    H.emit("palette-close");
  }
  function choose(r) {
    if (!r) return;
    closePal(false);
    if (typeof r.run === "function") { try { r.run(); } catch (e) { console.error("palette action", e); } return; }
    if (r.href) H.go(r.href);
  }
  H.palette = { open: openPal, close: closePal, isOpen: () => S.open, items: () => S.rows.map((r) => ({ group: r.group, type: r.type, name: r.name, href: r.href || "" })) };

  input.addEventListener("input", () => {
    S.q = input.value; S.sel = 0; refresh();
    clearTimeout(S.t); S.t = setTimeout(readSwitcher, 140);
  });
  input.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    if (e.key === "ArrowDown") { e.preventDefault(); if (S.rows.length) { S.sel = (S.sel + 1) % S.rows.length; draw(); } }
    else if (e.key === "ArrowUp") { e.preventDefault(); if (S.rows.length) { S.sel = (S.sel - 1 + S.rows.length) % S.rows.length; draw(); } }
    else if (e.key === "Home" && e.ctrlKey) { e.preventDefault(); S.sel = 0; draw(); }
    else if (e.key === "End" && e.ctrlKey) { e.preventDefault(); S.sel = Math.max(0, S.rows.length - 1); draw(); }
    else if (e.key === "Enter") { e.preventDefault(); const r = S.rows[S.sel]; if (r) choose(r); else if (S.q.trim()) { closePal(false); H.go(H.searchHref(S.q.trim())); } }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePal(); }
    else if (e.key === "Tab") { e.preventDefault(); if (S.rows.length) { S.sel = (S.sel + (e.shiftKey ? -1 : 1) + S.rows.length) % S.rows.length; draw(); } }
  });
  // The palette is modal: a page that grabs the focus while it is open (a conversation finishing its load) gets it back.
  input.addEventListener("blur", () => { setTimeout(() => { if (S.open && !root.contains(document.activeElement)) input.focus({ preventScroll: true }); }, 0); });
  list.addEventListener("click", (e) => { const a = e.target.closest && e.target.closest(".hm-pal-item"); if (!a) return; e.preventDefault(); choose(S.rows[Number(a.dataset.i)]); });
  list.addEventListener("mousemove", (e) => { const a = e.target.closest && e.target.closest(".hm-pal-item"); if (!a) return; const i = Number(a.dataset.i); if (i !== S.sel) { S.sel = i; list.querySelectorAll(".hm-pal-item").forEach((n) => n.classList.toggle("sel", n === a)); input.setAttribute("aria-activedescendant", "pal-opt-" + i); } });
  root.addEventListener("mousedown", (e) => { if (e.target === root) { e.preventDefault(); closePal(); } });
  close.addEventListener("click", () => closePal());
  // A late redirect (a thread that moved from Needs you to Threads) must not close a palette he has just reopened: choose() closes it itself.

  // ---------- the ways in ----------
  // Ctrl/Cmd-K opens it from anywhere, typing included. "/" opens it when no field has the keys.
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey && !e.shiftKey && (e.key === "k" || e.key === "K")) {
      if (!S.open && e.target && e.target.closest && e.target.closest(".hx-term-in")) return;   // the Manage terminal keeps its own Ctrl-K (clear line)
      e.preventDefault(); e.stopPropagation(); if (S.open) closePal(); else openPal(); return; }
    if (S.open) return;
    if (e.key === "/" && !mod && !e.altKey) {
      const a = document.activeElement;
      if (a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable)) return;
      e.preventDefault(); e.stopImmediatePropagation(); openPal();
    }
  }, true);
  // The top search box: a click opens the palette carrying what is typed there. Focus by keyboard or a script still types into the
  // box and Enter still goes to the search page (the form's own submit).
  const q = $("#q");
  if (q) {
    q.addEventListener("click", () => openPal(q.value));
    q.addEventListener("keydown", (e) => { if (e.key === "ArrowDown" && !e.isComposing) { e.preventDefault(); openPal(q.value); } });
  }
  const sf = $("#shell-search");
  if (sf) sf.addEventListener("pointerdown", (e) => { if (e.target.closest && e.target.closest(".search-icon, .search-key")) { e.preventDefault(); openPal(q ? q.value : ""); } });
})();
