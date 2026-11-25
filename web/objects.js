/* Helm objects: the object chip, the peek, and the Connected pane. Loaded after glyphs.js, before app.js.

   Helm.chip({ type, id, label, href, state, private })  -> <a class="hm-ochip t-<type>"> glyph + label (an anchor)
       type   ask note day chat task thread run job host (an unknown type draws as a note)
       id     what the type names: a thread id, a note path, a card id, a date ...; the peek reads it
       label  the text drawn (the id when none is given)
       href   where it opens (omitted: the chip is a plain label, still peekable)
       state  a quiet word the peek shows ("Waiting on you", "Running")
       private true: the chip carries the outlined Private mark and a dashed border, and the peek reads nothing about it
   Helm.chipHTML(spec) -> the same chip as a string (template-built markup; every field escaped).

   Helm.peek: a small preview card for any element that carries data-peek (every chip does).
       Helm.peek.attach(el, spec)      make another element peekable (spec as for chip)
       Helm.peek.show(el) / hide()     what hover, focus and long-press call
       Helm.peek.register(type, fn)    fn(spec) -> { title, sub, line, foot } or a promise of it; replaces the default for that type
     Desktop: hover or keyboard focus after 280 ms. Phone: a 450 ms long-press (the tap that follows is swallowed).
     The default readers use what the client already holds (threads, the board model) and one cheap read for a note.
     A private spec reads nothing: it shows its label, the Private mark and "Shown here only".

   Helm.connected: the right pane's one shape. Every page that has something to say about what is open draws it here.
       Helm.connected.render(sections, { lede, count, sub })     draws #pane-props (header "Connected", the count beside it) and returns the node
       Helm.connected.node(sections, opts)                       the same node, not yet placed
       sections: an array of
         { kind: "facts", rows: [[label, value]] }               a compact facts block; value is a string or a Node
         { title, type, count, items: [item], node, lede }       a group under a glyph heading
         item = { chip: spec | Node, line: string | Node, meta: string, node: Node }
       Every fact row is .conv-prop-row > .conv-prop-label + .conv-prop-val, and every group is .conv-prop-row >
       .conv-prop-label (its heading) + .conv-prop-val, so a page's own tests and styles keep finding rows by label. */
"use strict";
(() => {
  const H = window.Helm;
  if (!H || !H.glyph) return;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const TYPES = H.objectTypes || {};
  const typeOf = (t) => (TYPES[t] ? t : "note");
  const typeLabel = (t) => (TYPES[typeOf(t)] || {}).label || "Object";

  // ---------- chip ----------
  function chip(spec) {
    spec = spec || {};
    const type = typeOf(spec.type);
    const priv = !!spec.private;
    const a = document.createElement("a");
    a.className = "hm-ochip t-" + type + (priv ? " is-private" : "");
    if (spec.href) a.setAttribute("href", spec.href);
    const label = spec.label != null && spec.label !== "" ? String(spec.label) : (spec.id != null ? String(spec.id) : typeLabel(type));
    a.append(H.glyph(type), el("span", "hm-ochip-t", label));
    if (priv) { const m = el("span", "hm-priv"); m.append(H.glyph("private", 12), "Private"); a.append(m); }
    attach(a, Object.assign({}, spec, { type, label }));
    return a;
  }
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  function chipHTML(spec) { const d = document.createElement("div"); d.append(chip(spec)); return d.innerHTML; }

  // ---------- peek ----------
  const readers = {};
  const cache = new Map();            // note first lines: one cheap read per page, kept for the page's life
  const ago = (ts) => (typeof fmtAge === "function" ? fmtAge(ts) : "");
  const STATE_WORDS = { "needs-you": "Waiting on you", working: "In flight", "waiting-external": "On hold", backlog: "Backlog", done: "Done" };
  const clip = (s, n) => { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  const threadOf = (id) => (typeof threadById === "function" ? threadById(id) : null);

  readers.ask = readers.thread = (spec) => {
    const t = threadOf(spec.id);
    if (!t) return { title: spec.label, sub: spec.state || "", line: "Open it to read the whole thread." };
    const askT = typeof askedAt === "function" ? askedAt(t) : (t.last_change || 0);
    const bits = [STATE_WORDS[t.state] || t.state, t.category].filter(Boolean);
    return { title: t.title || spec.label, sub: bits.join(", "), line: clip(t.summary || (t.ask && t.ask.prompt) || "", 170) || "Open it to read the whole thread.", foot: askT ? (t.state === "needs-you" ? "Asked " : "Last change ") + ago(askT) : "" };
  };
  readers.task = readers.run = (spec) => {
    let c = null;
    try { const m = H.board && H.board.model && H.board.model(); for (const col of (m && m.columns) || []) for (const x of col.cards || []) if (x.id === spec.id) c = x; } catch { /* the board is not loaded */ }
    if (!c) return { title: spec.label, sub: spec.state || "", line: "Open it to see the whole card and what it connects to." };
    if (c.private) return { title: "Private card", sub: "", line: "Private: shown here only. Nothing of it is kept." };
    return { title: c.title || spec.label, sub: [c.status, c.assignee].filter(Boolean).join(", "), line: clip(c.body || "", 170) || "Open it to see the whole card and what it connects to.", foot: c.created_at ? "Added " + ago(c.created_at) : "" };
  };
  readers.note = readers.day = async (spec) => {
    const base = { title: spec.label, sub: spec.type === "day" ? "Journal page" : "Notebook page", line: "Open it to read the page and see what points at it." };
    if (!spec.id || !H.has || !H.has("notebook")) return base;
    const rel = String(spec.id);
    if (cache.has(rel)) return Object.assign(base, cache.get(rel));
    try {
      const r = await fetch("/api/notes/note?brief=1&rel=" + encodeURIComponent(rel), { credentials: "same-origin", redirect: "manual" });
      if (!r.ok) { cache.set(rel, {}); return base; }
      const j = await r.json();
      const out = { line: clip(j.excerpt || "", 170) || base.line };
      const folder = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
      if (folder) out.sub = (spec.type === "day" ? "Journal page, " : "Notebook page, ") + folder;
      cache.set(rel, out);
      return Object.assign(base, out);
    } catch { return base; }
  };
  readers.job = (spec) => ({ title: spec.label, sub: spec.state || "Scheduled job", line: "Open Manage to see when it runs and to pause it." });
  readers.chat = (spec) => ({ title: spec.label, sub: spec.state || "Chat session", line: "Open it to read the conversation." });
  readers.host = (spec) => ({ title: spec.label, sub: spec.state || "Host", line: "Open it to see what hangs from it." });

  let pk = null, showT = 0, hold = 0, cur = null, seq = 0;
  const node = () => {
    if (pk) return pk;
    pk = el("div", "hm-peek"); pk.id = "hm-peek"; pk.setAttribute("role", "tooltip");
    document.body.append(pk);
    return pk;
  };
  function attach(a, spec) {
    a.dataset.peek = typeOf(spec.type);
    if (spec.id != null) a.dataset.peekId = String(spec.id); else delete a.dataset.peekId;
    if (spec.label != null) a.dataset.peekLabel = String(spec.label);
    if (spec.state) a.dataset.peekState = String(spec.state); else delete a.dataset.peekState;
    if (spec.private) a.dataset.peekPrivate = "1"; else delete a.dataset.peekPrivate;
    return a;
  }
  const specOf = (a) => ({ type: a.dataset.peek, id: a.dataset.peekId, label: a.dataset.peekLabel || (a.textContent || "").trim(), state: a.dataset.peekState || "", private: a.dataset.peekPrivate === "1" });
  function place(a) {
    const p = node(), r = a.getBoundingClientRect(), w = Math.min(340, innerWidth - 24);
    p.style.width = w + "px";
    const x = Math.max(12, Math.min(r.left, innerWidth - w - 12));
    p.style.left = x + "px"; p.style.top = (r.bottom + 8) + "px";
    const ph = p.offsetHeight;
    if (r.bottom + 8 + ph > innerHeight - 8) p.style.top = Math.max(8, r.top - ph - 8) + "px";
  }
  function paint(spec, d) {
    const p = node(); p.replaceChildren();
    p.className = "hm-peek on t-" + typeOf(spec.type) + (spec.private ? " is-private" : "");
    const ty = el("div", "hm-peek-type"); ty.append(H.glyph(typeOf(spec.type)), typeLabel(spec.type));
    if (spec.private) { const m = el("span", "hm-priv"); m.append(H.glyph("private", 12), "Private"); ty.append(m); }
    p.append(ty, el("h5", "hm-peek-t", d.title || spec.label));
    if (d.sub) p.append(el("p", "hm-peek-sub", d.sub));
    if (d.line) p.append(el("p", "hm-peek-s", d.line));
    if (d.foot) p.append(el("div", "hm-peek-f", d.foot));
  }
  async function show(a) {
    if (!a || !a.isConnected) return;
    cur = a; const my = ++seq;
    const spec = specOf(a);
    // Private: nothing is read and nothing is copied; the mark and the label are all the peek says.
    const quick = spec.private
      ? { title: spec.label, sub: spec.state, line: "Private: shown here only. Nothing of it is kept." }
      : { title: spec.label, sub: spec.state, line: "" };
    paint(spec, quick); place(a);
    if (spec.private) return;
    const fn = readers[typeOf(spec.type)];
    if (!fn) return;
    let d = null;
    try { d = await fn(spec); } catch { d = null; }
    if (my !== seq || cur !== a || !d) return;
    paint(spec, d); place(a);
  }
  function hide() { clearTimeout(showT); clearTimeout(hold); seq++; cur = null; if (pk) pk.classList.remove("on"); }
  H.peek = { attach, show, hide, register: (type, fn) => { if (typeof fn === "function") readers[type] = fn; }, spec: specOf };
  H.chip = chip;
  H.chipHTML = chipHTML;

  const canHover = window.matchMedia ? window.matchMedia("(hover: hover)") : { matches: true };
  const peekable = (t) => (t && t.closest ? t.closest("[data-peek]") : null);
  document.addEventListener("mouseover", (e) => {
    if (!canHover.matches) return;
    const a = peekable(e.target); if (!a) return;
    if (e.relatedTarget && a.contains(e.relatedTarget)) return;
    clearTimeout(showT); showT = setTimeout(() => show(a), 280);
  });
  document.addEventListener("mouseout", (e) => {
    const a = peekable(e.target); if (!a) return;
    if (e.relatedTarget && a.contains(e.relatedTarget)) return;
    hide();
  });
  document.addEventListener("focusin", (e) => { const a = peekable(e.target); if (a && e.target.matches && e.target.matches(":focus-visible")) show(a); });
  document.addEventListener("focusout", (e) => { if (peekable(e.target)) hide(); });
  // Phone: a long press peeks, and the tap that ends it must not open the link.
  let lp = 0, held = null;
  document.addEventListener("touchstart", (e) => {
    const a = peekable(e.target); if (!a) return;
    clearTimeout(lp); held = null;
    lp = setTimeout(() => { held = a; show(a); }, 450);
  }, { passive: true });
  const endTouch = () => { clearTimeout(lp); if (held) { clearTimeout(hold); hold = setTimeout(hide, 2200); } };
  document.addEventListener("touchend", endTouch, { passive: true });
  document.addEventListener("touchmove", () => { clearTimeout(lp); }, { passive: true });
  document.addEventListener("touchcancel", () => { clearTimeout(lp); }, { passive: true });
  document.addEventListener("contextmenu", (e) => { if (peekable(e.target) && held) e.preventDefault(); });
  document.addEventListener("click", (e) => { const a = peekable(e.target); if (a && held === a) { e.preventDefault(); e.stopPropagation(); held = null; } }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("hashchange", hide);

  // ---------- Connected pane ----------
  const asNode = (v) => (v instanceof Node ? v : document.createTextNode(v == null ? "" : String(v)));
  function itemNode(it) {
    const row = el("div", "hm-conn-item conv-prop-link-item");
    if (it.node) { row.append(it.node); return row; }
    if (it.chip) row.append(it.chip instanceof Node ? it.chip : chip(it.chip));
    if (it.line) { const p = el("p", "hm-conn-line"); p.append(asNode(it.line)); row.append(p); }
    if (it.meta) row.append(el("div", "hm-meta", it.meta));
    return row;
  }
  function connectedNode(sections, opts) {
    opts = opts || {};
    const root = el("div", "hm-conn conv-props-body");
    if (opts.lede) root.append(el("p", "hm-conn-lede", opts.lede));
    for (const s of sections || []) {
      if (!s) continue;
      if (s.kind === "facts") {
        const rows = (s.rows || []).filter((r) => r && r[1] != null && r[1] !== "");
        if (!rows.length) continue;
        const dl = el("div", "hm-facts");
        for (const [k, v] of rows) {
          const r = el("div", "conv-prop-row hm-fact");
          r.append(el("span", "conv-prop-label", k));
          const val = el("span", "conv-prop-val"); val.append(asNode(v)); r.append(val);
          dl.append(r);
        }
        root.append(dl);
        continue;
      }
      const items = s.items || [];
      if (!s.node && !items.length) continue;
      const g = el("section", "hm-conn-group conv-prop-row");
      if (s.id) g.dataset.group = s.id;
      const gh = el("h4", "hm-conn-gh conv-prop-label");
      if (s.type) { const gl = H.glyph(typeOf(s.type)); gl.classList.add("t-" + typeOf(s.type)); gh.append(gl); }
      gh.append(s.title || "");
      if (s.count != null) gh.append(el("span", "hm-conn-n", String(s.count)));
      const body = el("div", "hm-conn-body conv-prop-val");
      if (s.lede) body.append(el("p", "hm-meta", s.lede));
      if (s.node) body.append(s.node);
      for (const it of items) body.append(itemNode(it));
      g.append(gh, body);
      root.append(g);
    }
    return root;
  }
  H.connected = {
    node: connectedNode,
    render(sections, opts) {
      opts = opts || {};
      const n = connectedNode(sections, opts);
      const c = opts.count;
      // The pane's header says "Connected"; the count, or the caller's own words, ride beside it.
      H.setProps(opts.sub || (typeof c === "number" ? c + (c === 1 ? " thing" : " things") : (c || "")), n);
      return n;
    },
  };
})();
