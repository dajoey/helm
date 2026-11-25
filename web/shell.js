/* Helm shell — the three-pane conversation app, one page at both sites.
   The top bar ("Helm", the site badge, the HUD, search, file a card, theme / sound / help)
   and three panes under it: the left nav lists every section with its count and holds the
   open section's list; the middle pane is the conversation of the selected thread, or a
   section's page; the right pane is its properties. Routes are hashes owned here; the
   modules draw into the hosts index.html declares (see SECTIONS). On a phone (≤ 760 px)
   the panes are a stack — nav, then list, then conversation, then properties — and the
   top bar's back button walks it. The keyboard map (KEYS,
   the `?` overlay). This file is a classic script so the inbox's globals (app.js) can read
   `Helm` synchronously; the notebook is imported dynamically so the CodeMirror bundle never
   loads on a site that has no notebook. The sections themselves are the registry's (modules.js:
   H.register), so a section is added by registering it, never by editing this file.        */
"use strict";

const Helm = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const typing = () => { const a = document.activeElement; return !!(a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable)); };
  const todayISO = () => { const d = new Date(); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* per-viewer convenience only */ } };
  const PHONE = window.matchMedia("(max-width: 760px)");

  const H = {
    site: null, modules: [], notesMode: "", meta: null, ready: null,
    route: { view: "needs-you" }, lastList: "#/needs-you",
    has: (m) => H.modules.includes(m),
    typing, todayISO, lsGet, lsSet,
    isPhone: () => PHONE.matches,
  };
  const listeners = {};
  H.on = (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); };
  H.emit = (ev, ...a) => { for (const fn of listeners[ev] || []) { try { fn(...a); } catch (e) { console.error(ev, e); } } };

  // ---------- the sections, in order, each with the module that switches it on ----------
  // list: the section holds a list in the left pane and opens a conversation in the middle
  // (a thread id rides in the route). page: the section is a page in the middle pane.
  // The registry (modules.js) holds them.
  const REG = window.HelmRegistry;
  Object.defineProperty(H, "SECTIONS", { get: () => REG.sorted() });
  const sections = () => H.SECTIONS.filter(s => H.has(s.module));
  H.sections = sections;
  const secOf = (key) => REG.get(key);
  H.register = (def) => REG.register(def);
  H.hooks = REG.hooks;
  H.features = REG.features;
  // A section that registers late (its file still arriving on a loaded host) must not leave the address
  // stranded: #/manage parsed while MANAGE was not yet registered falls to NEEDS YOU (parseHash's
  // default) and, with only a rail redraw, stays there after the file lands — the reload flake. So a
  // registration re-routes when the address names the section but the route does not.
  REG.onChange = () => {
    if (H.meta !== null || H.ready) { try { drawNav(); } catch { /* before the first draw */ } }
    const head = (location.hash.replace(/^#\/?/, "").split("?")[0].split("/")[0]) || "home";
    if (REG.get(head) && H.route.view !== head) navigate();
  };
  const counts = {};
  const alarms = {};   // key -> what is wrong ("" = nothing): a 'bad' mark on the row's count

  // ---------- site + modules (from /api/meta) ----------
  async function loadMeta() {
    let m = null;
    try {
      const r = await fetch("/api/meta", { credentials: "same-origin", redirect: "manual" });
      if (r.ok) m = await r.json();
    } catch { /* offline: the inbox's own banner says so */ }
    H.meta = m;
    H.modules = (m && Array.isArray(m.modules) && m.modules.length) ? m.modules.slice() : ["inbox", "conversations", "notebook", "activity"];
    if (!H.modules.includes("search")) H.modules.push("search");   // search is always there
    H.notesMode = (m && m.notes_mode) || "folder-readonly";
    // What only the web role knows (per-login modules).
    H.web = (m && m.web) || {};
    document.body.classList.toggle("has-notebook", H.has("notebook"));
    document.body.classList.toggle("notes-readonly", H.notesMode === "folder-readonly");
    drawNav();
    // The NOTEBOOK row's count (notes held) is wanted before the module is opened, and so are
    // the folders a thread's link may name a note by (the vault, and where it used to live:
    // an old card cites its minutes by a folder the notebook used to live in). "helm:vaults" tells a pane that
    // drew its links before they arrived to draw them again (app.js noteRelFromUrl).
    if (H.has("notebook")) fetch("/api/notes/meta", { credentials: "same-origin", redirect: "manual" }).then(r => r.ok ? r.json() : null)
      .then(j => {
        if (!j) return;
        if (j.notes) { const c = j.notes.notes != null ? j.notes.notes : j.notes; H.setCount("notebook", c); }
        H.vaults = [j.vault || j.root].concat(j.vault_aliases || []).filter(Boolean);
        window.dispatchEvent(new Event("helm:vaults"));
      }).catch(() => {});
    return m;
  }

  // ---------- the rail: every section with its count; the open section's list is the list column beside it ----------
  // Three widths:
  //   wide    > 900 px   the slim rail (icon over label), then the list column, the page and the Connected pane
  //   narrow  761-900    the same, the list and Connected columns narrower
  //   drawer  <= 760     the rows live in a drawer the bottom bar's More tab opens; the list column then holds the open
  //                      section's list, the pages stack as on any phone. The bar carries Today, Needs you, Chat, Notebook.
  const NARROW = window.matchMedia("(max-width: 900px)");
  const drawerMode = () => PHONE.matches;
  H.railMode = () => PHONE.matches ? "drawer" : NARROW.matches ? "narrow" : "wide";
  const railStatus = () => $("#rail-status");
  // Rail glyph per section (web/glyphs.js draws them; the fallback keeps the rail whole if that file is late).
  const GLYPH = { home: "home", "needs-you": "needs", notebook: "book", threads: "threads", activity: "activity" };
  const FALLBACK = '<path d="M7 6h12M7 12h12M7 18h8"/>';
  const sectionIcon = (key) => {
    const n = el("span", "nav-icon"); n.setAttribute("aria-hidden", "true");
    const body = H.glyphBody ? H.glyphBody(GLYPH[key] || "threads") : FALLBACK;
    n.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + body + "</svg>";
    return n;
  };
  // The rail draws one row per registered section.
  const TABS = ["home", "needs-you", "notebook"];
  function drawTabs(open) {
    const bar = $("#tabbar"), more = $("#rail-open");
    if (!bar || !more) return;
    bar.querySelectorAll("a.tab").forEach(n => n.remove());
    const have = sections();
    for (const key of TABS) {
      const s = have.find(x => x.key === key);
      if (!s) continue;
      const a = el("a", "tab" + (open === key ? " on" : ""));
      a.href = "#/" + key; a.dataset.tab = key; a.title = s.title;
      if (open === key) a.setAttribute("aria-current", "page");
      const gl = el("span", "tab-gl"); gl.setAttribute("aria-hidden", "true");
      gl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + (H.glyphBody ? H.glyphBody(GLYPH[key]) : FALLBACK) + "</svg>";
      a.append(gl, el("span", "tab-l", s.label));
      if (key === "needs-you") { const b = el("span", "tab-n"); b.id = "tab-count-needs-you"; a.append(b); }
      bar.insertBefore(a, more);
    }
    const g = more.querySelector(".tab-gl");
    if (g && !g.firstChild) g.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + (H.glyphBody ? H.glyphBody("more") : FALLBACK) + "</svg>";
    // More is the open one when the open section is not on the bar.
    const inBar = TABS.includes(open) && have.some(x => x.key === open);
    more.classList.toggle("on", !inBar && !!open);
    bar.style.setProperty("--tabs", String(bar.children.length));
    paintTabCount();
  }
  function paintTabCount() {
    const b = $("#tab-count-needs-you"); if (!b) return;
    const n = counts["needs-you"] || 0;
    b.textContent = n > 99 ? "99+" : String(n); b.hidden = !(n > 0);
  }
  function drawNav() {
    // The rows are redrawn each time; the list hosts (index.html) are kept, so the modules' references to them stay valid.
    const pane = $("#pane-left");
    const drawer = drawerMode();
    const rows = drawer ? $("#rail-rows") : $("#shell-rail-rows");
    document.querySelectorAll(".nav-sec, .rail-sep").forEach(n => n.remove());
    const open = viewOf(H.route);
    const shown = new Set();
    for (const s of sections()) {
      const row = el("a", "nav-sec" + (open === s.key ? " on" : ""));
      row.href = "#/" + s.key; row.dataset.sec = s.key; row.title = s.title;
      if (open === s.key) row.setAttribute("aria-current", "page"); else row.removeAttribute("aria-current");
      row.append(sectionIcon(s.key), el("span", "nav-label", s.label));
      const n = el("span", "nav-count", countText(s.key, counts[s.key]));
      n.id = "count-" + s.key;
      if (s.key === "needs-you") n.classList.toggle("lit", (counts[s.key] || 0) > 0);
      row.append(n);
      markAlarm(s.key, n);
      rows.append(row);
      // The open section's list shows in the list column; the others stay folded away.
      const host = $("#list-" + s.key);
      if (!host) continue;
      host.classList.toggle("hidden", open !== s.key);
      pane.append(host);
      shown.add(s.key);
    }
    // A list host of a section this site lacks is never shown.
    for (const host of document.querySelectorAll("#pane-left .nav-list")) if (!shown.has(host.dataset.sec)) host.classList.add("hidden");
    // A section with no list of its own (Today, a page section) leaves the list column empty: the page takes its width.
    const openSec = secOf(open);
    document.body.classList.toggle("no-list", !(openSec && (openSec.list || openSec.key === "notebook") && shown.has(open)));
    // The status cell (R6) is the foot of the rail, or of the drawer.
    const st = railStatus();
    if (st) (drawer ? $("#rail-drawer") : $("#shell-rail")).append(st);
    drawTabs(open);
    paintRail();
  }
  // The body says which rail is drawn (CSS and the tests read it); the drawer's open state follows the mode.
  function paintRail() {
    const mode = H.railMode();
    document.body.dataset.rail = mode;
    document.body.classList.toggle("rail-drawer", mode === "drawer");
    if (mode !== "drawer") closeRail(false);
  }
  function openRail() {
    if (!drawerMode()) return;
    document.body.classList.add("rail-open");
    $("#rail-open").setAttribute("aria-expanded", "true");
    $("#rail-drawer").setAttribute("aria-hidden", "false");
    const on = $("#rail-rows .nav-sec.on") || $("#rail-rows .nav-sec");
    if (on) on.focus({ preventScroll: true });
  }
  function closeRail(refocus) {
    if (!document.body.classList.contains("rail-open")) { const d = $("#rail-drawer"); if (d) d.setAttribute("aria-hidden", "true"); return; }
    document.body.classList.remove("rail-open");
    $("#rail-open").setAttribute("aria-expanded", "false");
    $("#rail-drawer").setAttribute("aria-hidden", "true");
    if (refocus) { $("#rail-open").focus({ preventScroll: true }); if (window.HelmTips) window.HelmTips.hide(); }   // focus returns to the control: no tip for it
  }
  H.openRail = openRail; H.closeRail = closeRail;
  $("#rail-open").addEventListener("click", () => { if (document.body.classList.contains("rail-open")) closeRail(true); else openRail(); });
  $("#rail-close").addEventListener("click", () => closeRail(true));
  $("#rail-scrim").addEventListener("click", () => closeRail(true));
  $("#rail-rows").addEventListener("click", (e) => { if (e.target.closest && e.target.closest("a.nav-sec")) closeRail(false); });
  $("#rail-drawer").addEventListener("click", (e) => { if (e.target.closest && e.target.closest("#rail-status")) closeRail(false); });
  // A rail count wider than two digits does not fit beside a centred icon: the badge shows "99+"
  // and the exact number stays readable — in the tooltip for sighted users, in the aria-label for readers.
  function countText(key, n) {
    if (n == null || key === "notebook") return "";
    return typeof n === "number" && n > 99 ? "99+" : String(n);
  }
  function paintCountTip(key, b) {
    if (!b) return;
    const row = b.closest(".nav-sec"); const label = row && row.querySelector(".nav-label");
    const v = counts[key]; if (v == null || !label) return;
    b.title = alarms[key] || (label.textContent + ", " + v);
    b.setAttribute("aria-label", label.textContent + ", " + v);
  }
  H.setCount = (key, n) => {
    counts[key] = n;
    const b = $("#count-" + key);
    // The notebook's count is its size, not something waiting on anyone: it is held for the page's header and not badged on the rail.
    if (b) { b.textContent = countText(key, n); if (key === "needs-you") b.classList.toggle("lit", n > 0); paintCountTip(key, b); }
    if (key === "needs-you") paintTabCount();
  };
  // A section's row says something is wrong without the section being open (the text is its tooltip).
  function markAlarm(key, b = $("#count-" + key)) {
    const on = alarms[key] || "";
    if (!b) return;
    b.classList.toggle("bad", !!on); paintCountTip(key, b);
    let pip = b.previousElementSibling && b.previousElementSibling.classList.contains("nav-pip") ? b.previousElementSibling : null;
    if (on && !pip) { pip = el("span", "nav-pip"); b.before(pip); }
    if (pip) { if (on) pip.title = on; else pip.remove(); }
  }
  H.setAlarm = (key, text) => { alarms[key] = text || ""; markAlarm(key); };
  H.setNeeds = (n) => H.setCount("needs-you", n);   // app.js's name for it
  H.count = (key) => counts[key];

  // ---------- routes ----------
  const dec = (s) => { try { return decodeURIComponent(s); } catch { return s; } };
  const TID = "[A-Za-z0-9._:-]+";
  function parseHash(h) {
    h = h == null ? location.hash : h;
    let m;
    // Deep links: a client's "Open in Helm" uses #t=<id>; an older build used #conv-<id>.
    if ((m = new RegExp("^#t=(" + TID + ")$").exec(h)) || (m = new RegExp("^#conv-(" + TID + ")$").exec(h))) return { view: "conv", tid: m[1] };
    // Old routes carry `canon`, the address navigate() swaps in (location.replace) so bookmarks and
    // the mobile app's links land on the three-pane app's own addresses.
    const body = h.replace(/^#\/?/, "");
    const qi = body.indexOf("?");
    const pathPart = qi >= 0 ? body.slice(0, qi) : body;
    const params = new URLSearchParams(qi >= 0 ? body.slice(qi + 1) : "");
    const parts = pathPart.split("/");
    if (!parts[0]) return { view: "home", params, canon: "#/home" };   // an empty address is Today, on every site
    const head = parts[0];
    const rest = parts.slice(1).join("/");
    const listRoute = (view) => {
      const r = { view, params };
      if (parts[1]) { r.tid = dec(parts[1]); if (parts[2] === "props") r.props = true; }
      return r;
    };
    // A section registered with a route callback (CHAT, BOARD, MANAGE): #/<key>/<part>/<part>...
    const reg = REG.get(head);
    if (reg && typeof reg.route === "function") return { view: head, parts: parts.slice(1).filter(Boolean).map(dec), params };
    switch (head) {
      case "needs-you": return listRoute("needs-you");
      case "home": return { view: "home", params };
      case "threads": return listRoute("threads");
      case "activity": return listRoute("activity");
      // the old shell's routes and the web UI's (bookmarks, the mobile app's links)
      case "inbox": return rest ? { view: "conv", tid: dec(rest) } : { view: "needs-you", params, canon: "#/needs-you" };
      case "backlog": return { view: "threads", params: new URLSearchParams("state=backlog"), canon: "#/threads?state=backlog" };
      case "conv": case "thread": return rest ? { view: "conv", tid: dec(rest) } : { view: "needs-you", params, canon: "#/needs-you" };
      // the retired agent console's pages (old bookmarks, the mobile app's links)
      case "agents": case "board": case "frontdesk": case "settings": return { view: "needs-you", params: new URLSearchParams(), canon: "#/needs-you" };
      // the notebook's routes; ?folder= is the explicit root folder,
      // distinct from the visual notebook home and its phone ?nav=1 rail.
      case "notebook": case "folder": return { view: "notebook", kind: "folder", folder: params.has("folder") ? dec(params.get("folder") || "") : (rest ? dec(rest) : null), params };
      case "day": return { view: "notebook", kind: "day", date: /^\d{4}-\d{2}-\d{2}$/.test(rest) ? rest : todayISO(), params };
      case "note": return { view: "notebook", kind: "note", rel: dec(rest) };
      case "edit": return { view: "notebook", kind: "note", rel: dec(rest), edit: true };
      case "topic": return { view: "notebook", kind: "topic", rel: dec(rest) };
      case "search": return { view: "search", q: params.get("q") != null ? params.get("q") : (rest ? dec(rest) : ""), params };
      default: return { view: "needs-you", params };
    }
  }
  H.parseHash = parseHash;
  // An attachment's event is written for the agent that must open it; a person reads the
  // file's name and size (helm/conversation.py human_attachment, the same rule).
  H.humanBody = (kind, body) => {
    const s = String(body == null ? "" : body);
    if (!/-attachment$/.test(kind || "")) return s;
    const m = /^FILE ATTACHED -- OPEN IT BEFORE YOU ANSWER:\s*(.+?) \(([^,()]+), ([^)]+)\)\s*(?:\n([\s\S]*))?$/.exec(s.trim());
    if (!m) return s;
    const rest = (m[4] || "").split("\n").filter(l => l.trim() && !/^(https?:\/\/|\/)/.test(l.trim()));
    return "📎 " + m[1] + " (" + m[3] + ")" + (rest.length ? "\n" + rest.join("\n") : "");
  };
  // The section a route belongs to (the left-pane row that is "on").
  const viewOf = (r) => r.view === "conv" ? "threads" : r.view === "search" ? viewOf(parseHash(H.lastList)) : r.view;
  H.go = (hash) => { if (location.hash === hash) navigate(); else location.hash = hash; };
  H.replace = (hash) => { if (location.hash === hash) navigate(); else location.replace(hash); };
  H.searchHref = (q) => "#/search?q=" + encodeURIComponent(q);
  H.noteHref = (rel) => "#/note/" + encodeURIComponent(rel);
  H.dayHref = (d) => "#/day/" + d;
  H.threadsHref = (params) => { const p = new URLSearchParams(params || {}); const s = p.toString(); return "#/threads" + (s ? "?" + s : ""); };
  // A conversation opens inside the section it was reached from (needs-you, threads, activity)
  // so the list stays beside it; anywhere else it opens under THREADS, the section that holds every thread.
  H.convHref = (tid, sec) => {
    const s = sec || (["needs-you", "threads", "activity"].includes(H.route.view) ? H.route.view : "threads");
    const qs = s === H.route.view && H.route.params ? H.route.params.toString() : "";
    return "#/" + s + "/" + encodeURIComponent(tid) + (qs ? "?" + qs : "");
  };
  H.propsHref = (r) => "#/" + r.view + "/" + encodeURIComponent(r.tid) + "/props" + (r.params && r.params.toString() ? "?" + r.params.toString() : "");
  H.listHref = (r) => "#/" + r.view + (r.params && r.params.toString() ? "?" + r.params.toString() : "");

  // ---------- panes ----------
  // Every .main-view in the middle pane is one of these: the one named is shown (a registered section's page included).
  let mainTransition = 0, activeAnimations = null;
  function showMain(name) {
    const views = Array.from(document.querySelectorAll("#pane-main > .main-view"));
    const next = $("#view-" + name);
    const token = ++mainTransition;
    // Superseding a transition cancels its actual animation effects, not only its token.
    if (activeAnimations) {
      activeAnimations.outgoing.cancel(); activeAnimations.incoming.cancel();
      activeAnimations = null;
    }
    for (const n of views) if (n.classList.contains("view-leaving")) { n.classList.add("hidden"); n.classList.remove("view-leaving", "view-entering"); }
    const current = views.find(n => !n.classList.contains("hidden") && n !== next);
    for (const n of views) if (n !== next && n !== current) { n.classList.add("hidden"); n.classList.remove("view-entering"); }
    if (!next) return;
    next.classList.remove("hidden", "view-leaving", "view-entering");
    document.body.dataset.main = name;
    const reduce = document.hidden || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!current || reduce || typeof current.animate !== "function" || typeof next.animate !== "function") {
      if (current) current.classList.add("hidden");
      return;
    }
    current.classList.add("view-leaving");
    next.classList.add("view-entering");
    const outgoing = current.animate([
      { opacity: 1, transform: "translateX(0) scale(1)" },
      { opacity: 0, transform: "translateX(-12px) scale(.995)" },
    ], { duration: 160, easing: "ease-in-out", fill: "forwards" });
    const incoming = next.animate([
      { opacity: 0, transform: "translateX(12px) scale(.995)" },
      { opacity: 1, transform: "translateX(0) scale(1)" },
    ], { duration: 180, easing: "ease-out", fill: "forwards" });
    activeAnimations = { outgoing, incoming };
    let completed = false;
    const finish = () => {
      if (completed || token !== mainTransition) return;
      completed = true;
      activeAnimations = null;
      current.classList.add("hidden");
      current.classList.remove("view-leaving");
      next.classList.remove("view-entering");
      outgoing.cancel(); incoming.cancel();
    };
    Promise.allSettled([outgoing.finished, incoming.finished]).then(finish);
    // Content never depends on a background-tab animation reaching its last frame.
    setTimeout(finish, 240);
  }
  H.showMain = showMain;
  // The right pane: a conversation's properties (conversation.js fills #props-body), a page's own
  // side panel (the notebook's topic aside), or nothing.
  // The pane's header is always "Connected" (web/objects.js: H.connected draws what the page points at); a caller's own
  // title ("Status", "Search", "Topic") rides beside it as a quiet sub-line, and the plain "Properties" says nothing extra.
  H.setProps = (title, node) => {
    const sub = $("#props-sub"); if (sub) sub.textContent = title && title !== "Properties" ? title : "";
    const body = $("#props-body"); body.replaceChildren();
    if (node) body.append(node);
    document.body.classList.toggle("props-has", !!node);   // a section that draws no right pane (noProps) gets one while it holds something
    document.body.classList.toggle("props-empty", !node && $("#aside").classList.contains("hidden"));
  };
  H.propsBody = () => $("#props-body");
  const propsHidden = () => lsGet("helm.props") === "0";
  const paintProps = () => { document.body.classList.toggle("props-hidden", propsHidden()); const b = $("#btn-props"); if (b) b.classList.toggle("on", !propsHidden()); };
  $("#btn-props").addEventListener("click", () => { lsSet("helm.props", propsHidden() ? "1" : "0"); paintProps(); });
  $("#props-close").addEventListener("click", () => { if (H.isPhone()) H.back(); else { lsSet("helm.props", "0"); paintProps(); } });
  paintProps();

  // The page sections whose own list is a nav of their page (a phone shows the page first; Back shows the list).
  const PAGES = [];
  // On a phone the route picks the pane of the stack: the list (nav + the open section's list),
  // the middle (a conversation or a page), or the properties. Wide screens show all three.
  function paintPane(r) {
    const s = secOf(r.view);
    let pane = "left";
    if (s && typeof s.route === "function") {
      // A registered section (CHAT, BOARD, MANAGE): its list first on a phone when it has one, else its page.
      pane = s.list && (r.parts || []).length <= (s.listParts || 0) ? "left" : "main";   // CHAT: #/chat/<profile> is still the list
      document.body.dataset.pane = pane;
      const back = $("#shell-back");
      // A page with no list (Board, Manage) has nothing above it to go back to until a card or a sub-page is open.
      back.classList.toggle("hidden", pane === "left" || (!s.list && !(r.parts || []).length));
      back.title = "Back to the list";
      return;
    }
    if (r.props) pane = "props";
    else if (r.tid || (s && s.page && !(s.list && !r.tid && r.view !== "notebook")) || r.view === "search" || r.view === "notebook") pane = "main";
    // The notebook home is a real page now; its folder rail is the explicit ?nav=1 stack entry.
    if (r.view === "notebook" && r.kind === "folder" && r.folder === null && r.params && r.params.get("nav") === "1") pane = "left";
    if (PAGES.includes(r.view)) pane = r.props ? "props" : "main";
    // A page section's own list (its nav) on a phone: #/<page>?nav=1, where Back from the page goes.
    if (PAGES.includes(r.view) && !r.props && !r.tid && r.params && r.params.get("nav") === "1") pane = "left";
    // ACTIVITY's rows are the list: on a phone they come first, and its summary page (heartbeat,
    // counts, the runs that went quiet) is one tap away at #/activity?summary=1.
    if (r.view === "activity" && !r.tid) pane = r.props ? "props" : (r.params && r.params.get("summary") === "1") ? "main" : "left";
    document.body.dataset.pane = pane;
    const back = $("#shell-back");
    back.classList.toggle("hidden", pane === "left" || (r.view === "home"));   // Today is a top-level page: nothing above it
    back.title = pane === "props" ? "Back to the conversation" : "Back to the list";
  }
  H.back = () => {
    const r = H.route;
    // A page's side pane has no thread: back is the page itself.
    if (r.props) { H.go(r.tid ? H.convHref(r.tid, r.view) : "#/" + r.view); return; }
    if (r.tid) { H.go(H.listHref(r)); return; }
    if ((r.parts || []).length) { H.go("#/" + r.view); return; }   // a registered section's page: back is its top
    if (r.view === "notebook") {
      if (H.isPhone() && r.kind === "folder" && r.folder === null && !(r.params && r.params.get("nav") === "1")) { H.go("#/notebook?nav=1"); return; }
      H.go("#/notebook"); return;
    }
    // A page section on a phone: back shows its list pane, not the page again.
    if (H.isPhone() && PAGES.includes(r.view) && !(r.params && r.params.get("nav") === "1")) { H.go("#/" + r.view + "?nav=1"); return; }
    H.go("#/" + (secOf(r.view) ? r.view : "needs-you"));
  };
  $("#shell-back").addEventListener("click", () => H.back());

  // One navigation at a time; a hash that changes while one is in flight (the notebook's
  // views load asynchronously) is not dropped but run again once the current one settles.
  let navigating = false, queued = false, activeSec = null;
  async function navigate() {
    await H.ready;
    if (navigating) { queued = true; return; }
    let r = parseHash();
    // A section this site does not have falls back to NEEDS YOU (a bookmark from the other site).
    const sec = secOf(r.view);
    if (sec && !H.has(sec.module)) { location.replace("#/needs-you"); return; }
    if (r.view === "notebook" && !H.has("notebook")) { location.replace("#/needs-you"); return; }
    if (r.canon && location.hash !== r.canon) { location.replace(r.canon); return; }
    if (r.view === "conv") {
      // An old-style link to a thread: open it under NEEDS YOU when it is lit, else under THREADS.
      const lit = typeof state !== "undefined" && (state.threads || []).some(t => t.thread_id === r.tid && t.state === "needs-you");
      location.replace(H.convHref(r.tid, lit ? "needs-you" : "threads"));
      return;
    }
    navigating = true;
    try {
      H.route = r;
      if (secOf(r.view) && !r.tid) H.lastList = location.hash || "#/needs-you";
      const s = secOf(r.view);
      document.body.dataset.view = r.view;
      document.body.classList.toggle("notebook-home", r.view === "notebook" && r.kind === "folder" && r.folder === null);
      closeRail(false);
      // The registered sections' own life: leaving one unmounts it, entering one mounts it once.
      if (activeSec && activeSec !== r.view) { const o = secOf(activeSec); if (o && typeof o.unmount === "function") { try { o.unmount(); } catch (e) { console.error("unmount", e); } } }
      const entering = !!s && activeSec !== r.view;
      activeSec = s ? r.view : null;
      document.body.classList.toggle("no-props", !!(s && s.noProps));
      if (entering || !(s && s.noProps)) document.body.classList.remove("props-has");
      paintPane(r);
      drawNav();
      if (entering && s && typeof s.mount === "function") {
        try { s.mount({ list: $("#list-" + s.key), view: s.page ? $("#view-" + s.page) : null }, r); } catch (e) { console.error("mount", e); }
      }
      if (r.tid) {
        showMain("conv");
        if (typeof window.openConversation === "function") window.openConversation(r.tid, r.view);
      } else {
        if (typeof window.closeConversation === "function") window.closeConversation();
        if (s && s.page) showMain(s.page);
        else if (r.view === "search") showMain("search");
        else showMain("empty");
        if (r.view === "notebook") await notebook().then(nb => nb && nb.navigate(r)).catch(e => { console.error(e); showNotebookError(e); });
        else $("#aside").classList.add("hidden");
        if (s && typeof s.route === "function") { try { s.route(r.parts || [], r); } catch (e) { console.error("route", e); } }
        if (r.view === "search") { const q = $("#q"); if (q && q.value !== r.q) q.value = r.q || ""; }
      }
      H.emit("route", r);
    } finally { navigating = false; if (queued) { queued = false; navigate(); } }
  }
  H.navigate = navigate;
  // The middle pane's empty state for a list section with nothing selected.
  H.emptyMain = (big, sub, kind) => {
    const v = $("#view-empty"); v.replaceChildren();
    const e = el("div", "empty " + (kind || "")); e.append(el("span", "big", big)); if (sub) e.append(el("span", "sub", sub)); v.append(e);
  };

  // ---------- the notebook module, loaded on first use ----------
  let nbPromise = null;
  function notebook() {
    if (!H.has("notebook")) return Promise.resolve(null);
    if (!nbPromise) nbPromise = import("/notebook/app.js").then(m => { m.init(H); return m; });
    return nbPromise;
  }
  H.notebook = notebook;
  function showNotebookError(e) {
    const main = $("#main"); if (main) main.innerHTML = '<div class="view"><div class="empty"><b>The notebook did not load.</b><br>' + String(e && e.message || e).replace(/</g, "&lt;") + "</div></div>";
  }

  // ---------- brand link ----------
  // The brand opens Today. The markup keeps its literal (app/web.py swaps the app name inside it), so the address is set here.
  { const b = $(".shell-brand"); if (b) { b.setAttribute("href", "#/home"); b.title = b.title.replace(/what needs you$/, "today"); } }

  // ---------- search box ----------
  $("#shell-search").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#q").value.trim();
    if (q) H.go(H.searchHref(q));
  });

  // ---------- keys (the `?` overlay lists them) ----------
  const KEYS = [
    ["Everywhere", [
      ["?", "this sheet"], ["Ctrl+K", "the command palette: jump anywhere, or do anything"], ["/", "open the command palette"], ["Esc", "close the sheet, the lightbox, the switcher, a menu, or this"],
      ["R", "refresh now (the lists re-read; the notebook re-reads)"],
      ["N", "file a card"],
      ["F", "answers first: jump to NEEDS YOU and open the first ask"],
      ["E", "expand every section list and every block in the conversation"], ["C", "collapse them"],
      ["Ctrl+O", "jump to a note or a thread (the search box where there is no notebook)"],
      ["Ctrl+D", "today's day page"], ["Alt+Shift+N", "file a card, from anywhere, the editor included"],
    ]],
    ["Conversation", [
      ["Enter", "in the composer: send (Shift+Enter for a new line); on a focused answer button: submit. Space is deliberately dead"],
      ["Ctrl+Enter", "in the composer: send the note only (no option picked). In the day page's append box: append."],
      ["Esc", "on a phone: back to the list"],
    ]],
    ["Notebook", [
      ["Ctrl+E", "edit the note you are reading; again to go back to reading it"],
      ["Ctrl+S", "save now (the note also saves itself a moment after you stop typing)"],
      ["Ctrl+L", "in the editor: tick or untick the line, or turn it into a checkbox line"],
      ["[[", "in the editor: link to a note by name; ![[ embeds a file. Paste or drop an image to attach it."],
      ["Ctrl+click", "in the editor: open the link under the pointer"],
      ["Alt+N", "new note in the folder you are in (store mode only; the wiki is read-only at home)"],
    ]],
  ];
  H.KEYS = KEYS;
  function openHelp() {
    closeHelp();
    const o = el("div", "overlay shell-help-overlay");
    const box = el("div", "sw help");
    box.append(el("h3", null, "Keys"));
    // The rail's sections, as the registry holds them (the module that switches one off drops it from here too).
    const secs = [["Sections (the left rail, in order)", sections().map(s => [s.label, s.title])]];
    for (const [title, rows] of secs.concat(KEYS)) {
      box.append(el("h4", null, title));
      const t = el("table");
      for (const [k, d] of rows) { const tr = el("tr"); const td1 = el("td"); td1.append(el("kbd", null, k)); tr.append(td1, el("td", null, d)); t.append(tr); }
      box.append(t);
    }
    box.append(el("p", "muted", "One workspace: NEEDS YOU opens answers first, every card is a conversation, and notes link back to the work around them. Folds and drafts are remembered."));
    o.append(box);
    document.body.append(o);
    o.addEventListener("click", (e) => { if (e.target === o) closeHelp(); });
  }
  function closeHelp() { document.querySelectorAll(".shell-help-overlay").forEach(o => o.remove()); }
  H.openHelp = openHelp; H.closeHelp = closeHelp;
  $("#btn-help").addEventListener("click", openHelp);

  // E / C: every section list open or folded, and every collapsible block in the conversation.
  H.expandAll = () => { H.emit("expand", true); document.querySelectorAll("#view-conv details").forEach(d => { d.open = true; }); };
  H.collapseAll = () => { H.emit("expand", false); document.querySelectorAll("#view-conv details").forEach(d => { d.open = false; }); };
  // F: answers first — NEEDS YOU, first ask open.
  H.answersFirst = () => {
    const first = typeof needsYou === "function" ? needsYou()[0] : null;
    H.go(first ? H.convHref(first.thread_id, "needs-you") : "#/needs-you");
  };

  document.addEventListener("keydown", (ev) => {
    if (ev.defaultPrevented) return;
    if (ev.key === "Escape") {
      if ($(".shell-help-overlay")) { closeHelp(); return; }
      if (document.body.classList.contains("rail-open")) { closeRail(true); return; }
      if (H.isPhone() && document.body.dataset.pane !== "left" && !$("#sheet:not(.hidden)") && !$("#lightbox:not(.hidden)")) { H.back(); return; }
    }
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && !ev.altKey && (ev.key === "o" || ev.key === "O")) {
      ev.preventDefault();
      if (H.has("notebook")) notebook().then(nb => nb && nb.openSwitcher());
      else { $("#q").focus(); $("#q").select(); }
      return;
    }
    if (mod && !ev.altKey && (ev.key === "d" || ev.key === "D") && H.has("notebook")) { ev.preventDefault(); H.go(H.dayHref(todayISO())); return; }
    if (ev.altKey && ev.shiftKey && (ev.key === "n" || ev.key === "N")) { ev.preventDefault(); if (typeof openSheet === "function") openSheet(); return; }
    if (ev.altKey && !mod && (ev.key === "n" || ev.key === "N") && H.has("notebook")) { ev.preventDefault(); notebook().then(nb => nb && nb.newNoteHere()); return; }
    if (typing() || mod || ev.altKey) return;
    if (ev.key === "/") { ev.preventDefault(); $("#q").focus(); $("#q").select(); return; }
    if (ev.key === "?") { ev.preventDefault(); openHelp(); return; }
    const k = ev.key.toLowerCase();
    if (k === "r") { H.refresh(); }
    else if (k === "n") { if (typeof openSheet === "function") openSheet(); }
    else if (k === "e") H.expandAll();
    else if (k === "c") H.collapseAll();
    else if (k === "f") H.answersFirst();
  });
  // R and the ↻ button: the inbox re-sorts and re-reads, the notebook re-reads, every module hears "refresh".
  H.refresh = () => {
    if (typeof resort === "function") resort();
    if (document.body.dataset.view === "notebook") notebook().then(nb => nb && nb.refresh());
    H.emit("refresh");
  };
  $("#refresh").addEventListener("click", () => H.refresh());

  // ---------- layout: the panes sit under the shell bar ----------
  const top = $("#shell-top");
  const setH = () => document.documentElement.style.setProperty("--shell-h", top.offsetHeight + "px");
  if (window.ResizeObserver) { const ro = new ResizeObserver(setH); ro.observe(top); }
  H.measureShell = setH;
  setH();
  // A resize between the three widths (a fold, a rotation) redraws the rail and the panes where they stand.
  const resized = () => { drawNav(); paintPane(H.route); H.emit("resize"); };
  PHONE.addEventListener("change", resized);
  NARROW.addEventListener("change", resized);

  // ---------- resizable columns: drag or arrow keys on the list and Connected edges; kept as helm.paneL / helm.paneR ----------
  // The widths go on the root as --user-left / --user-props; helm.css's .panes reads them ahead of the breakpoint defaults.
  const PANE_KEY = { left: "helm.paneL", props: "helm.paneR" };
  const PANE_VAR = { left: "--user-left", props: "--user-props" };
  const PANE_MIN = 200;
  function paneMax(which) {
    const other = which === "left" ? $("#pane-props") : $("#pane-left");
    const otherW = other && other.offsetWidth ? other.offsetWidth : 0;
    const rail = $("#shell-rail");
    const room = window.innerWidth - (rail ? rail.offsetWidth : 0) - otherW - 380;   // the page keeps at least 380 px
    return Math.max(PANE_MIN, Math.min(560, room));
  }
  const paneClamp = (which, w) => Math.round(Math.max(PANE_MIN, Math.min(paneMax(which), w)));
  function paneApply(which, w, save) {
    if (w == null) { document.documentElement.style.removeProperty(PANE_VAR[which]); if (save) lsSet(PANE_KEY[which], ""); }
    else {
      w = paneClamp(which, w);
      document.documentElement.style.setProperty(PANE_VAR[which], w + "px");
      if (save) lsSet(PANE_KEY[which], String(w));
    }
    const h = $("#rz-" + which);
    if (h) {
      const pane = $(which === "left" ? "#pane-left" : "#pane-props");
      h.setAttribute("aria-valuenow", String(Math.round((pane && pane.offsetWidth) || w || 0)));
      h.setAttribute("aria-valuemin", String(PANE_MIN)); h.setAttribute("aria-valuemax", String(Math.round(paneMax(which))));
    }
    return w;
  }
  function paneInit() {
    const panes = $("#panes"); if (!panes) return;
    for (const which of ["left", "props"]) {
      const h = el("div", "pane-resizer " + (which === "left" ? "rz-l" : "rz-r"));
      h.id = "rz-" + which; h.tabIndex = 0;
      h.setAttribute("role", "separator"); h.setAttribute("aria-orientation", "vertical");
      h.setAttribute("aria-label", which === "left" ? "Resize the list column" : "Resize the Connected pane");
      h.title = "Drag to resize. Arrow keys nudge, Home resets.";
      panes.append(h);
      const saved = parseInt(lsGet(PANE_KEY[which]) || "", 10);
      if (saved > 0) paneApply(which, saved, false);
      const cur = () => { const p = $(which === "left" ? "#pane-left" : "#pane-props"); return p ? p.offsetWidth : 0; };
      h.addEventListener("pointerdown", (e) => {
        if (e.button) return;
        e.preventDefault(); h.setPointerCapture(e.pointerId); h.classList.add("drag"); document.body.classList.add("resizing");
        const move = (ev) => {
          const r = panes.getBoundingClientRect();
          const w = which === "left" ? ev.clientX - r.left - ($("#shell-rail") ? $("#shell-rail").offsetWidth : 0) : r.right - ev.clientX;
          paneApply(which, w, false);
        };
        const done = () => {
          h.removeEventListener("pointermove", move); h.removeEventListener("pointerup", done); h.removeEventListener("pointercancel", done);
          h.classList.remove("drag"); document.body.classList.remove("resizing");
          paneApply(which, cur(), true); H.emit("resize");
        };
        h.addEventListener("pointermove", move); h.addEventListener("pointerup", done); h.addEventListener("pointercancel", done);
      });
      h.addEventListener("keydown", (e) => {
        const step = e.shiftKey ? 48 : 16;
        // the left edge grows to the right; the Connected edge grows to the left
        const dir = which === "left" ? 1 : -1;
        let w = null;
        if (e.key === "ArrowRight") w = cur() + dir * step;
        else if (e.key === "ArrowLeft") w = cur() - dir * step;
        else if (e.key === "Home") { e.preventDefault(); paneApply(which, null, true); H.emit("resize"); return; }
        else return;
        e.preventDefault(); paneApply(which, w, true); H.emit("resize");
      });
      h.addEventListener("dblclick", () => { paneApply(which, null, true); H.emit("resize"); });
    }
  }
  paneInit();
  H.panes = { apply: paneApply, min: PANE_MIN, max: paneMax };
  // A narrower window pulls a saved width back inside what fits.
  window.addEventListener("resize", () => { for (const which of ["left", "props"]) { const v = parseInt(lsGet(PANE_KEY[which]) || "", 10); if (v > 0) paneApply(which, v, false); } });

  // ---------- start ----------
  H.ready = loadMeta();
  window.addEventListener("hashchange", navigate);
  if (!location.hash) location.replace("#/home");
  H.ready.then(navigate);
  return H;
})();
window.Helm = Helm;
