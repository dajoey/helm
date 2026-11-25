// The notebook module: a page as an ES module inside the Helm shell, drawn in the Chart Room
// design. Home (today's page, what changed, what is in play, the fleet atlas, the shelves), the note page (outline,
// latest entries, the Connected pane), the topic, the day as a ship's log, folders, the editor (store mode only;
// read-only at home where the wiki is the truth), the quick switcher (Ctrl+O), rename with its dry run, attachments.
// The shell (shell.js) owns the routes and the top bar and calls navigate(); the inbox is the web UI's (app.js), so
// Its inbox / backlog / thread views are gone from here and a thread opens as its conversation overlay
// (#/conv/<id>). The rail and the header are drawn from the same state; a refresh that rebuilds the view never
// touches what is being typed. Every link to another object is a chip (Helm.chip) and the right pane is
// Helm.connected, so the notebook, the asks, the board and the days read as one place.

import {api, upload, esc, richText, age, clock, localDate, shortDate, toast,
        lsGet, lsSet, open, closedGroups, saveOpen, noteHref, threadHref, dayHref,
        folderHref, folderFromRoute, editHref, topicHref, fileHref, setVault, noteRelFromUrl, appName,
        chipHtml, glyphHtml, stemOf, DATE_RE, noteChip, dayChip, threadChip, taskChip, plural, kb, readMins,
        startOfToday, longDate, monthName, whenParts, stripMd, whoName, lineStamp} from "./lib.js";
import {threadHtml, cardAction, snapshotInputs, restoreInputs} from "./card.js";

// The editor bundle loads the first time a note is opened for editing.
let editorMod = null;
const loadEditorModule = async () => editorMod || (editorMod = await import("./editor.js"));

const REFRESH_MS = 120000;

let H = null;   // the shell (window.Helm)
const readOnly = () => !!(H && H.notesMode === "folder-readonly");
const isHome = () => !!(H && H.site !== "work");
const TASK_ID = /\bt_[0-9a-f]{8}\b/g;

const S = {
  route: {view: "folder", folder: null},
  meta: null,            // /api/notes/meta, when the route exists
  folders: null,         // /api/notes/folders
  foldersErr: null,
  refreshedAt: 0,
  view: null,            // payload of the current view
  viewErr: null,
  timer: null,
  refreshing: false,
  gen: 0,                // bumped by every navigate(): an answer for an older route is dropped
  gone: null,            // the note view the site answered 404 for (nothing to poll for)
  editing: false,        // the note view is in edit mode (the route says so)
  editor: null,          // the mounted editor, while one is on screen
  editRel: null,
  ebar: null,            // payload behind the bar above the editor (conflict, credential, draft)
  wired: false,
  linksIn: new Map(),    // rel -> distinct notes linking to it, learned from the graph read on home
  focus: "",             // an element id to scroll to once the view is drawn
  atlasAll: false,       // the atlas shows every host, not the busiest few
};

const $ = id => document.getElementById(id);
const main = () => $("main");
const html2el = s => { const d = document.createElement("div"); d.innerHTML = s; return d.firstElementChild; };
// The live thread list the shell already holds (the web UI's app.js state); empty when it is not there.
const liveThreads = () => { try{ return typeof state !== "undefined" && state && Array.isArray(state.threads) ? state.threads : []; }catch(e){ return []; } };

// --- Data ------------------------------------------------------------------------------
async function loadFolders(){
  try{ S.folders = await api("/api/notes/folders"); S.foldersErr = null; }
  catch(e){ S.folders = null; S.foldersErr = e.status === 404 ? "the notebook routes are not up yet" : e.message; }
  return S.folders;
}
async function loadMeta(){
  try{ S.meta = await api("/api/notes/meta"); setVault(S.meta.vault || S.meta.root, S.meta.vault_aliases); }catch(e){ S.meta = null; }
}

// Full refresh: notebook folders and the current view's own data.
// The inputs are read after the awaits, right before the redraw, so text typed while the requests
// were out is in the snapshot. A navigate() that happened meanwhile (S.gen moved) owns the screen:
// this refresh's payload is dropped and it draws nothing.
export async function refresh(poll){
  if(S.refreshing) return;
  S.refreshing = true;
  const gen = S.gen;
  try{
    await Promise.all([loadFolders(), loadMeta()]);
    await loadView(poll, gen);
    if(gen !== S.gen) return;
    S.refreshedAt = Date.now();
    const snap = snapshotInputs([main(), $("aside")]);
    render();
    restoreInputs(snap);
  }catch(e){
    toast("refresh failed: " + e.message);
  }finally{
    S.refreshing = false;
    armTimer();
  }
}
function armTimer(){
  clearTimeout(S.timer);
  S.timer = setTimeout(() => { if(document.body.dataset.view === "notebook") refresh(true); else armTimer(); }, REFRESH_MS);
}


// Home's feeds are independent: a failed feed must not hide the rest, and a failed day must not hide the
// shelves. All settle together (one call each, in parallel, no per-note reads) before the one generation check in
// loadView, so a late home answer cannot paint a new route. A feed the site does not have yet (404) is quiet.
async function homeRead(path){
  try{ return {value: await api(path), error: null, status: 200}; }
  catch(e){ return {value: null, error: e && e.message ? e.message : String(e), status: e && e.status || 0}; }
}
async function loadHome(){
  const today = localDate(new Date());
  const [recent, day, changes, graph, tasks, atlas] = await Promise.all([
    homeRead("/api/notes/recent?limit=12"),
    homeRead("/api/day?date=" + encodeURIComponent(today)),
    homeRead("/api/notes/changes?since=" + Math.floor(startOfToday()) + "&limit=200"),
    homeRead("/api/notes/graph?limit=12"),
    homeRead("/api/notes/tasks?limit=20"),
    isHome() ? homeRead("/api/notes/atlas") : Promise.resolve({value: null, error: null, status: 0}),
  ]);
  const v = {home: true, recent: recent.value, recentErr: recent.error,
             day: day.value, dayErr: day.error,
             changes: changes.value, changesErr: changes.status === 404 ? null : changes.error,
             graph: graph.value, tasks: tasks.value, atlas: atlas.value};
  if(graph.value && Array.isArray(graph.value.most_linked)){
    for(const m of graph.value.most_linked) S.linksIn.set(m.rel, m.links_in);
  }
  return v;
}

// What a note's page also wants: who last wrote it, its latest entries when it is an append-style page, and the board
// cards on the threads about it. None of it blocks the first paint: loadView draws from the note and the topic, then
// enrichView() reads these and redraws once they land. A failure leaves its part out.
const LOGGY = /\b(log|changelog|ops)\b/i;
const EMPTY_X = () => ({history: [], tail: null, board: []});
const xCache = new Map();            // rel -> the last extras read, drawn at once while the next read is out
async function noteExtras(note, ctxP){
  const quiet = p => p.catch(() => null);
  const heads = []; const m = /<h[1-6][^>]*>([\s\S]*?)<\/h[1-6]>/gi; let mm;
  while((mm = m.exec(note.html || ""))) heads.push(mm[1].replace(/<[^>]*>/g, "").trim());
  const lastHead = heads.length ? heads[heads.length - 1] : "";
  const appendy = (note.size || 0) > 20480 || LOGGY.test(lastHead);
  // history and the tail only need the note, so they run beside the topic call; the board cards wait for the threads.
  const histP = quiet(api("/api/notes/history?rel=" + encodeURIComponent(note.rel)));
  const tailP = appendy ? quiet(api("/api/notes/tail?rel=" + encodeURIComponent(note.rel) + "&n=6")) : Promise.resolve(null);
  const ctx = await ctxP;
  const threads = ctx && !ctx.error ? [].concat(ctx.open || [], ctx.settled || []).slice(0, 6) : [];
  const [history, tail] = await Promise.all([histP, tailP]);
  return {history: history && Array.isArray(history.revisions) ? history.revisions : [], tail, board: []};
}
// After the first paint: read the extras and draw again if they changed anything and the page is still this one.
function enrichView(view, gen){
  if(!view || !view.note) return;
  noteExtras(view.note, Promise.resolve(view.ctx)).then(x => {
    xCache.set(view.note.rel, x);
    if(S.gen !== gen || S.view !== view) return;
    if(JSON.stringify(x) === JSON.stringify(view.x)) return;
    view.x = x;
    const snap = snapshotInputs([main(), $("aside")]);
    render();
    restoreInputs(snap);
  }).catch(() => { /* the page stays as drawn */ });
}

// Per-view payload.
// A note the site does not hold (deleted, moved without a redirect, a stale link in a tab left
// open) is not going to appear because the page asked again: the timer's refresh (poll) leaves it
// alone. Opening a route, R and the refresh button still ask.
async function loadView(poll, gen){
  const r = S.route;
  const key = r.view + ":" + r.rel;
  if(poll && S.gone === key && !S.view) return;
  let view = null, viewErr = null, gone = null;
  try{
    if(r.view === "day") view = await api("/api/day?date=" + encodeURIComponent(r.date));
    else if(r.view === "note" || r.view === "topic"){
      let note;
      try{ note = await api("/api/notes/note?rel=" + encodeURIComponent(r.rel)); }
      catch(e){ if(e.status === 404) gone = key; throw e; }
      let raw = null;
      const wantRaw = r.view === "note" && S.editing && !(S.editor && S.editRel === r.rel);
      const [ctx, rawBody] = await Promise.all([
        api("/api/topic?rel=" + encodeURIComponent(r.rel)).catch(e => ({error: e.message})),
        wantRaw ? api("/api/notes/raw?rel=" + encodeURIComponent(r.rel)) : Promise.resolve(null),
      ]);
      raw = rawBody;
      view = {note, ctx, raw, x: xCache.get(note.rel) || EMPTY_X()};   // the extras come after the first paint (enrichView)
    }
    else if(r.view === "folder" && r.folder === null) view = await loadHome();
    else if(r.view === "folder") view = await api("/api/notes/list?folder=" + encodeURIComponent(r.folder));
  }catch(e){
    view = null; viewErr = e.status === 404 && !S.folders ? "The notebook routes are not up yet." : e.message;
  }
  // The route moved while the requests were out (the user opened something else): this answer is
  // for a view that is no longer the page, and must not become S.view under the new route.
  if(gen !== undefined && gen !== S.gen) return;
  S.gone = gone;
  S.view = view; S.viewErr = viewErr;
  if(view && view.note && gen !== undefined) enrichView(view, gen);
}

// What is in play: the notes that live threads link to, with what is open on each (asks, threads, board cards and
// open checkboxes). Read from what the shell already holds; no request.
function inPlay(tasksFeed){
  const pages = new Map();
  const get = rel => { if(!pages.has(rel)) pages.set(rel, {rel, asks: [], threads: [], cards: [], open: 0}); return pages.get(rel); };
  const threads = liveThreads().filter(t => t && t.state !== "done");
  const byThread = new Map();
  for(const t of threads){
    for(const l of (t.links || [])){
      const rel = noteRelFromUrl(l && l.url);
      if(!rel) continue;
      const p = get(rel);
      (t.state === "needs-you" ? p.asks : p.threads).push(t);
      if(!byThread.has(t.thread_id)) byThread.set(t.thread_id, []);
      byThread.get(t.thread_id).push(p);
    }
  }
  const byRel = new Map(((tasksFeed && tasksFeed.notes) || []).map(n => [n.rel, n]));
  for(const p of pages.values()) p.open = (byRel.get(p.rel) || {}).open || 0;
  const title = rel => stemOf(rel);
  return [...pages.values()].sort((a, b) => (b.asks.length - a.asks.length) || ((b.threads.length + b.cards.length) - (a.threads.length + a.cards.length)) || title(a.rel).localeCompare(title(b.rel)));
}

// --- Rendering: rail -------------------------------------------------------------------
function drawRail(){
  const r = S.route;
  const f = S.folders;
  const notes = S.meta && S.meta.notes ? (S.meta.notes.notes ?? S.meta.notes) : (f && f.stats ? f.stats.notes : null);
  const today = localDate(new Date());
  const homeOn = r.view === "folder" && r.folder === null;
  const row = (href, cls, name, n, title) => '<a class="rrow' + (cls ? ' ' + cls : '') + '" href="' + href + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' +
    '<span class="name">' + esc(name) + '</span>' + (n === "" || n == null ? '' : n) + '</a>';
  let h = '<div class="nbx-rail-head"><h2>Notebook</h2><span class="grow"></span>' +
          '<span class="stamp' + (S.foldersErr ? ' down' : '') + '" title="last read">' + (S.foldersErr ? esc(S.foldersErr) : esc(clock(S.refreshedAt / 1000))) + '</span>' +
          '<button class="quiet" data-nbrefresh="1" title="refresh now (R)" aria-label="Refresh">&#8635;</button></div>';
  h += '<div class="nb-tools"><span>' + (notes != null ? esc(notes) + ' notes' : 'notebook') + '</span>' +
       (readOnly() ? '<span class="nb-readonly" title="read-only at this site (a staging copy or a rollback): every write is refused here">read-only</span>' : '') + '</div>';
  h += row("#/notebook", homeOn ? "on" : "", "Home");
  const open_ = f && f.today ? (f.today.exists ? f.today.open_tasks || 0 : null) : null;
  h += row(dayHref(today), r.view === "day" && r.date === today ? "on" : "", "Today",
           '<span class="n' + (open_ ? ' lit' : '') + '" title="' + (f && f.today && !f.today.exists ? 'no note yet today' : 'open checkbox lines') + '">' +
           (f && f.today ? (f.today.exists ? (open_ ? esc(open_) : esc(shortDate(today))) : 'no note') : esc(shortDate(today))) + '</span>');
  if(f && f.previous){
    h += row(dayHref(f.previous.date), "sub", shortDate(f.previous.date) + ", open items",
             '<span class="n lit week">' + esc(f.previous.open_tasks) + '</span>', "carry-over from " + f.previous.date);
  }
  const play = inPlay(S.view && S.view.home ? S.view.tasks : null).length;
  h += row("#/notebook?play=1", "", "In play", play ? '<span class="n">' + play + '</span>' : '', "pages that open asks, threads or tasks point at");
  h += '<div class="rsec"><span>Shelves</span><a href="#/notebook" title="all folders">all</a></div>';
  if(f){
    const rootLast = f.folders.filter(fo => fo.folder).concat(f.folders.filter(fo => !fo.folder));
    for(const fo of rootLast){
      const sub = fo.folder.includes("/");
      const label = fo.folder ? (sub ? fo.folder.split("/").pop() : fo.folder) : "Root pages";
      const on = (r.view === "folder" && r.folder === fo.folder) || ((r.view === "note" || r.view === "topic") && folderOf(r.rel) === fo.folder);
      h += row(folderHref(fo.folder), (sub ? 'sub ' : '') + (on ? 'on ' : '') + (COLD.test(fo.folder) ? 'cold' : ''), label, '<span class="n">' + esc(fo.count) + '</span>');
    }
  }else{
    h += '<div class="rempty">' + esc(S.foldersErr || "loading") + '</div>';
  }
  $("rail").innerHTML = h;
}
const COLD = /^(archive|trash|\.trash)(\/|$)/i;
const touchy = () => { try{ return !!(window.matchMedia && window.matchMedia('(hover: none)').matches); }catch(e){ return false; } };
// The folder a note lives in: the longest folder the index lists that prefixes it, else the
// first segment.
function folderOf(rel){
  rel = String(rel || "");
  let best = "";
  for(const fo of ((S.folders && S.folders.folders) || [])){
    if(rel.startsWith(fo.folder + "/") && fo.folder.length > best.length) best = fo.folder;
  }
  if(best) return best;
  return rel.includes("/") ? rel.split("/")[0] : "";
}

// --- Shared pieces ---------------------------------------------------------------------------------
function propsHtml(fm){
  const keys = Object.keys(fm || {});
  if(!keys.length) return "";
  return '<div class="props">' + keys.map(k => '<span class="k">' + esc(k) + '</span><span>' + richText(Array.isArray(fm[k]) ? fm[k].join(", ") : fm[k]) + '</span>').join("") + '</div>';
}
// A note the site does not hold: the page chrome, the path it was looked for at, and the ways back.
function missingView(rel, view){
  const fo = folderOf(rel);
  const stem = stemOf(rel);
  const folderCrumb = fo ? '<a href="' + folderHref(fo) + '">' + esc(fo) + '</a><span>/</span>' : '';
  return '<div class="view nb-note-view nbx-missing"><div class="nbx-note-main"><div class="crumb hm-crumb"><a href="#/notebook">Notebook</a><span>/</span>' + folderCrumb + '<span>' + esc(stem) + '</span></div>' +
    '<h1 class="nbx-title">This page is not here</h1>' +
    '<p class="nbx-missing-path"><code>' + esc(rel) + '</code></p>' +
    '<p class="nbx-lede">It may have been renamed, moved or deleted, or the link that brought you here is out of date.</p>' +
    '<p class="nbx-missing-go"><a class="btn primary" href="#/notebook">Back to the notebook</a> <button type="button" class="btn secondary" data-pal="1">Find a page</button></p></div></div>';
}
function emptyBlock(msg){ return '<div class="view"><div class="empty">' + esc(msg) + '</div></div>'; }
// A log line for display: plain words, bare addresses linked, card ids as chips.
function lineHtml(text){
  const t = stripMd(text);
  let out = "", last = 0, m;
  TASK_ID.lastIndex = 0;
  while((m = TASK_ID.exec(t))){
    out += richText(t.slice(last, m.index)) + taskChip(m[0], m[0]);
    last = m.index + m[0].length;
  }
  return out + richText(t.slice(last));
}
function appendBox(d){
  if(readOnly()) return "";
  return '<div class="appendbox nbx-jin"><span class="nbx-jin-gl" aria-hidden="true">' + glyphHtml("pen", 19) + '</span>' +
         '<textarea id="append-line" rows="1" aria-label="Add to ' + (d.today ? 'today' : esc(d.date)) + '" placeholder="Add to ' + (d.today ? 'today' : esc(d.date)) + '"></textarea>' +
         '<button class="send btn primary sm" data-append="1">Add line</button>' +
         '<span class="hint">' + (d.today ? 'Goes on the end of the note, word for word, as many lines as you type. Ctrl+Enter sends it.'
                                          : 'A past day is history: it takes text on the end and nothing else about it changes. Ctrl+Enter sends it.') + '</span></div>';
}
// A line that opens with a clock time ("6:05 PM text") sets the time apart from the words.
function stampTimes(root, doc){
  root.querySelectorAll('p, li').forEach(n => {
    const t = n.firstChild;
    if(t && t.nodeType === 3){
      const m = /^\s*(\d{1,2}:\d{2}(?:\s?[AP]M)?)\s+/i.exec(t.nodeValue);
      if(m){ const el = doc.createElement('time'); el.className = 'nbx-jt'; el.textContent = m[1]; t.nodeValue = t.nodeValue.slice(m[0].length); n.insertBefore(el, t); }
    }
  });
}
// The journal lines of a daily note's rendered page: its text blocks without the checkbox lines, newest last.
function journalHtml(html, keep){
  const doc = new DOMParser().parseFromString('<div id="r">' + (html || '') + '</div>', 'text/html');
  const root = doc.getElementById('r');
  root.querySelectorAll('li.task, h1').forEach(n => n.remove());
  root.querySelectorAll('ul, ol').forEach(l => { if(!l.children.length) l.remove(); });
  stampTimes(root, doc);
  const blocks = [...root.children].filter(n => n.textContent.trim());
  const shown = keep ? blocks.slice(-keep) : blocks;
  return {html: shown.map(n => n.outerHTML).join(""), more: blocks.length - shown.length, n: blocks.length};
}
function dayHtml(html){
  const doc = new DOMParser().parseFromString('<div id="r">' + (html || '') + '</div>', 'text/html');
  const root = doc.getElementById('r');
  stampTimes(root, doc);
  return root.innerHTML;
}

// --- Rendering: day, a ship's log --------------------------------------------------------------------
const KIND_GLYPH = {answered: "ask", noted: "pen", settled: "check", filed: "thread", run: "run", woke: "job", snoozed: "job", park: "job", capped: "thread", dispatch: "thread"};
function dayLog(d){
  const tl = (d.timeline || []).slice().reverse();
  let h = '<section class="nbx-log tl"><h3 class="nbx-h">Threads that day</h3>';
  if(!tl.length) return h + '<p class="muted nbx-quiet">nothing moved</p></section>';
  h += '<ol class="hm-log">';
  if(d.today) h += '<li class="hm-log-now" aria-label="now"><span class="hm-log-t">Now</span><i></i><b></b></li>';
  for(const e of tl){
    const parts = (e.parts || []).map(p => p.label);
    const who = parts.length > 1 ? parts.join(", then ") : "";
    const n = e.count > 1 ? ' <span class="n" title="' + e.count + ' threads with this title, moved together; the link is the first">&times;' + e.count + '</span>' : '';
    const g = KIND_GLYPH[e.kind] || "thread";
    h += '<li class="hm-log-entry tle t-' + (g === "ask" ? "ask" : g === "run" ? "run" : g === "job" ? "job" : "thread") + '"><div class="hm-log-t">' + esc(clock(e.ts)) + '</div>' +
         '<div class="hm-log-nd"><i>' + glyphHtml(g, 13) + '</i></div><div class="hm-log-bd">' +
         '<span class="k ' + esc(e.kind) + '"' + (who ? ' title="' + esc(who) + '"' : '') + '>' + esc(e.label) + '</span> ' +
         chipHtml({type: "thread", id: e.thread_id, label: e.title, href: threadHref(e.thread_id)}) + n +
         (e.detail ? (e.raw ? '<div class="d raw"><span title="the option as ' + appName() + ' recorded it; its wording was not kept">' + richText(e.detail) + '</span></div>'
                             : '<div class="d">' + richText(e.detail) + '</div>') : '') +
         (e.more || []).map(m => '<div class="d more">' + richText(m) + '</div>').join("") +
         '</div></li>';
  }
  return h + '</ol></section>';
}
function dayView(){
  const d = S.view;
  if(!d) return emptyBlock(S.viewErr || "loading");
  const ro = readOnly();
  const isToday = d.date === localDate(new Date());
  let h = '<div class="view nb-day"><div class="dayhead"><div class="dayhead-t"><div class="hm-crumb"><a href="#/notebook">Notebook</a><span>/</span><span>Days</span></div>' +
          '<h1 class="nbx-day-title">' + esc(longDate(d.date)) + ' <span class="muted">' + esc(d.date) + '</span></h1></div>' +
          '<span class="nav"><a class="btn quiet sm" href="' + dayHref(d.prev) + '" title="previous day" aria-label="Previous day">&lsaquo;</a>' +
          '<a class="btn quiet sm" href="' + dayHref(localDate(new Date())) + '" title="today (Ctrl+D)">Today</a>' +
          '<a class="btn quiet sm" href="' + dayHref(d.next) + '" title="next day" aria-label="Next day">&rsaquo;</a></span>' +
          '<span class="dayhead-links">' + (d.exists ? '<a class="muted" href="' + noteHref(d.rel) + '" title="open as a note">' + esc(d.rel) + '</a>' +
                      (d.today && !ro ? ' <a href="' + editHref(d.rel) + '" title="Ctrl+E">Edit</a>' : '')
                    : '<span class="muted">No note yet for this day</span>' +
                      (d.today && !ro ? ' <button class="quiet btn sm" data-newday="1" title="from the Daily template">Create today\'s note</button>' : '')) +
          '</span></div><div class="daygrid"><div class="nbx-day-main">';
  h += '<div class="note">' + propsHtml(d.frontmatter) + '<div class="md">' + (dayHtml(d.html) || (d.exists ? '<p class="muted">empty</p>' :
       '<div class="nbx-nopage"><p class="nbx-nopage-t">' + (ro ? 'No page for this day' : 'Nothing written for this day yet') + '</p>' +
       '<p class="nbx-nopage-s">' + (ro ? 'This site is read-only, so a page cannot be started here.' : d.today ? 'Add a line below, or create the page from the Daily template.' : 'A past day takes text on the end; add a line below to start its page.') + '</p></div>')) + '</div>';
  h += appendBox(d) + '</div>';
  if(d.mentions && d.mentions.length){
    h += '<p class="nbx-chips"><span class="muted">Links to</span> ' + d.mentions.map(m => noteChip(m.rel, m.title)).join(" ") + '</p>';
  }
  if(d.changed && d.changed.length){
    // Home: the wiki pages that changed that day (the profile's Day view).
    h += '<div class="carry nbx-changed"><h3 class="nbx-h">Pages that changed</h3><ul>' + d.changed.map(m => '<li>' + noteChip(m.rel, m.title || m.rel) + '</li>').join("") + '</ul></div>';
  }
  h += '</div><div class="nbx-day-side">';
  h += dayLog(d);
  if(d.calls && d.calls.length){
    h += '<div class="calls"><h3 class="nbx-h">Calls</h3>';
    for(const c of d.calls){
      h += '<div class="callrow">' + chipHtml({type: "thread", id: c.thread_id, label: c.title, href: threadHref(c.thread_id)}) +
           (c.recording ? '<a class="sp" href="' + esc(c.recording.url) + '" target="_blank" rel="noopener">' + esc(c.recording.label || ("Recorder #" + c.recording.n)) + '</a>' : '') +
           ' <span class="muted">' + esc(clock(c.ts)) + '</span></div>';
    }
    h += '</div>';
  }
  if(d.today && d.carry && d.carry.tasks && d.carry.tasks.length){
    h += '<div class="carry"><h3 class="nbx-h">Still open from ' + esc(shortDate(d.carry.date)) + '</h3><ul>';
    for(const t of d.carry.tasks){
      h += '<li><span>' + esc(t.text) + '</span><button class="btn quiet sm" data-mkthread="' + esc(t.text) + '" data-mkrel="' + esc(d.carry.rel) +
           '" title="file a card with this line">Make this a card</button></li>';
    }
    h += '</ul><div class="muted nbx-quiet">Read from <a href="' + dayHref(d.carry.date) + '">that note</a>; nothing here changes it.</div></div>';
  }
  return h + '</div></div></div>';
}

// --- Rendering: notebook home ----------------------------------------------------------------------
function homeStat(label, value, tail){
  return '<div><dt>' + esc(label) + '</dt><dd>' + (value == null ? '&mdash;' : esc(value)) +
         (tail ? '<span>' + esc(tail) + '</span>' : '') + '</dd></div>';
}
function modifiedHtml(ts){
  if(!ts || !Number.isFinite(Number(ts))) return '';
  const d = new Date(Number(ts) * 1000);
  return '<time datetime="' + esc(d.toISOString()) + '" title="' + esc(d.toLocaleString()) + '">' + esc(age(ts)) + '</time>';
}
function sectionHead(id, title, sub, extra){
  return '<div class="nbx-sh"><div><h2 id="' + id + '" class="nbx-h2">' + title + '</h2>' + (sub ? '<p class="nbx-sub">' + sub + '</p>' : '') + '</div>' + (extra || '') + '</div>';
}

// Today's page: the date, the journal lines, the open checkboxes, the box that adds to it.
function homeToday(v, f){
  const today = localDate(new Date());
  const d = v.day;
  const tasks = d && Array.isArray(d.tasks) ? d.tasks.filter(t => !t.done) : [];
  const openCount = d ? (d.open_tasks == null ? tasks.length : d.open_tasks) : (f && f.today ? f.today.open_tasks : null);
  let h = '<section class="nb-today nbx-paper" aria-labelledby="nb-today-title"><p class="nbx-paper-k"><span class="nb-live-dot" aria-hidden="true"></span>' + glyphHtml("day", 16) + ' Today’s page</p>' +
          '<h2 id="nb-today-title" class="nbx-paper-h"><a href="' + dayHref(today) + '">' + esc(longDate(today)) + '</a></h2>';
  if(v.dayErr){
    h += '<p class="nb-inline-error" role="status">Today could not be loaded. ' + esc(v.dayErr) + '</p>' +
         '<a class="nb-text-link" href="' + dayHref(today) + '">Open today&rsquo;s page</a>';
    if(f && f.previous && f.previous.open_tasks) h += '<a class="nb-text-link" href="' + dayHref(f.previous.date) + '">' + esc(f.previous.open_tasks) + ' carried over from ' + esc(shortDate(f.previous.date)) + '</a>';
    return h + '</section>';
  }
  if(!d) return h + '<p class="nb-quiet">Loading today&rsquo;s page&hellip;</p></section>';
  if(!d.exists){
    h += '<p class="nb-quiet">No daily note yet. The page is ready when you are.</p>';
    if(!readOnly()) h += '<button type="button" class="nb-secondary-action btn secondary" data-newday="1">Create today&rsquo;s note</button>';
  }else{
    const jl = journalHtml(d.html, 5);
    h += '<p class="nbx-fn">' + dayChip(d.date) + (openCount == null ? '' : ' <span class="muted">' + esc(openCount) + ' open</span>') + '</p>';
    if(jl.n) h += '<div class="md nbx-journal">' + jl.html + '</div>' +
                  (jl.more ? '<a class="nb-text-link" href="' + dayHref(d.date) + '">' + esc(jl.more) + ' earlier ' + (jl.more === 1 ? 'entry' : 'entries') + ' on the page</a>' : '');
    if(openCount){
      const dis = readOnly() || !d.today ? ' disabled title="' + (readOnly() ? 'read-only at this site; tick it in the live ' + esc(appName()) + ' notebook' : 'history: a past day\'s note accepts only appends') + '"' : '';
      h += '<ul class="nb-task-list nbx-todo">' + tasks.slice(0, 4).map(t => '<li><label><input type="checkbox" class="nbx-box" data-hometask="' + esc(t.line) + '"' + dis + '><span>' + esc(t.text) + '</span></label></li>').join('') + '</ul>';
      if(openCount > tasks.slice(0, 4).length) h += '<a class="nb-text-link" href="' + dayHref(d.date) + '">See all ' + esc(openCount) + ' on today&rsquo;s page</a>';
    }else if(!jl.n) h += '<p class="nb-quiet">No open checkbox lines. A clear page for today.</p>';
    h += appendBox(d);
  }
  if(d.carry && Array.isArray(d.carry.tasks) && d.carry.tasks.length){
    h += '<div class="nb-carry nbx-carry"><h3 class="nbx-h">Carried from <a href="' + dayHref(d.carry.date) + '">' + esc(shortDate(d.carry.date)) + '</a></h3>' +
         '<ul class="nb-task-list">' + d.carry.tasks.slice(0, 3).map(t => '<li><a href="' + dayHref(d.carry.date) + '"><span class="nb-task-mark" aria-hidden="true"></span><span>' + esc(t.text) + '</span></a></li>').join('') + '</ul>';
    if(d.carry.tasks.length > 3) h += '<a class="nb-text-link" href="' + dayHref(d.carry.date) + '">All ' + esc(d.carry.tasks.length) + ' carried-over tasks</a>';
    h += '</div>';
  }else if(f && f.previous && f.previous.open_tasks){
    h += '<a class="nb-text-link nb-carry" href="' + dayHref(f.previous.date) + '">' + esc(f.previous.open_tasks) + ' open from ' + esc(shortDate(f.previous.date)) + '</a>';
  }
  const timeline = Array.isArray(d.timeline) ? d.timeline.slice(-4).reverse() : [];
  h += '<div class="nb-dayline nbx-dayline"><h3 class="nbx-h">Latest activity</h3>';
  if(!timeline.length) h += '<p class="nb-quiet">No thread activity yet today.</p>';
  else{
    h += '<ol class="nb-timeline-list">';
    for(const e of timeline){
      h += '<li><time>' + esc(clock(e.ts)) + '</time><div><span class="nb-timeline-kind">' + esc(e.label) + '</span> ' +
           chipHtml({type: "thread", id: e.thread_id, label: e.title, href: threadHref(e.thread_id)}) + '</div>' +
           (e.count > 1 ? '<span class="nb-timeline-count" title="' + esc(e.count) + ' threads moved together">&times;' + esc(e.count) + '</span>' : '') + '</li>';
    }
    h += '</ol>';
  }
  return h + '</div><a class="nb-text-link" href="' + dayHref(d.date) + '">Open the full day</a></section>';
}

// Changed today: the pages Helm wrote to today, newest first, with the last lines and who wrote them; then the pages
// that changed some other way (an edit outside Helm) from the recent list.
function changedGroups(v){
  const groups = [], byRel = new Map();
  const rows = v.changes && Array.isArray(v.changes.changes) ? v.changes.changes : [];
  for(const c of rows){
    let g = byRel.get(c.rel);
    if(!g){ g = {rel: c.rel, title: c.title, ts: c.ts, lines: [], outside: false}; byRel.set(c.rel, g); groups.push(g); }
    const who = whoName(c.principal, c.source);
    const ls = (c.lines || []).slice().reverse();
    for(const l of ls) if(g.lines.length < 3 && stripMd(l)) g.lines.push({who, text: l});
  }
  const t0 = startOfToday();
  const recent = v.recent && Array.isArray(v.recent.notes) ? v.recent.notes : [];
  for(const n of recent){
    if(!n || !n.rel || byRel.has(n.rel) || !(n.mtime >= t0)) continue;
    const g = {rel: n.rel, title: n.title || n.stem, ts: n.mtime, lines: [], outside: true};
    byRel.set(n.rel, g); groups.push(g);
  }
  return groups;
}
function homeChanged(v){
  const groups = changedGroups(v);
  let h = '<section class="nbx-chg" aria-labelledby="nb-changed-title">' + sectionHead("nb-changed-title", "Changed today", "Agents add to the notebook all day. Newest lines, grouped by page.");
  if(v.changesErr) h += '<p class="nb-inline-error" role="status">What changed today could not be read. ' + esc(v.changesErr) + '</p>';
  if(!groups.length) return h + '<p class="nb-quiet">Nothing has changed in the notebook yet today.</p></section>';
  const item = g => {
    const p = whenParts(g.ts);
    return '<div class="nbx-chg-i"><div class="nbx-chg-h">' + noteChip(g.rel, g.title) + '<time>' + esc(p.time) + '</time></div>' +
      g.lines.map(l => '<p class="nbx-ln">' + (l.who ? '<b>' + esc(l.who) + '</b>' : '') + lineHtml(l.text) + '</p>').join('') +
      (g.outside ? '<p class="nbx-ln nbx-out">Edited outside ' + esc(appName()) + '.</p>' : '') + '</div>';
  };
  h += groups.slice(0, 5).map(item).join('');
  if(groups.length > 5) h += '<details class="nbx-more"><summary>' + (groups.length - 5) + ' more pages changed</summary>' + groups.slice(5, 30).map(item).join('') + '</details>';
  return h + '</section>';
}

// In play: the pages that live asks, threads and board cards point at, each with chips of what is open.
function homePlay(v){
  const pages = inPlay(v.tasks);
  const cap = 8;
  let h = '<section class="nbx-play block" id="nb-inplay" aria-labelledby="nb-play-title">' + sectionHead("nb-play-title", "In play", "Pages that open asks, threads or tasks point at. Close the loop and they fall back to the shelves.");
  if(!pages.length) return h + '<p class="nb-quiet">Nothing is open against a page right now.</p></section>';
  h += '<ul class="play">';
  for(const p of pages.slice(0, cap)){
    const why = p.asks.map(threadChip).concat(p.threads.map(threadChip),
      p.cards.map(c => taskChip(c.id, c.private ? "Private card" : (c.title || c.id), c.private ? {private: true} : {state: c.status})),
      p.open ? ['<span class="hm-pill soft">' + esc(plural(p.open, "open box", "open boxes")) + '</span>'] : []);
    h += '<li>' + noteChip(p.rel) + '<div class="why">' + why.join(' ') + '</div></li>';
  }
  h += '</ul>';
  if(pages.length > cap) h += '<p class="nbx-quiet muted">and ' + (pages.length - cap) + ' more</p>';
  return h + '</section>';
}

// The shelves: one spine per folder, taller for more pages; the cold shelves (Archive, Trash) are dashed.
const SPINE_COLORS = ["--c-note", "--c-day", "--c-chat", "--c-task", "--c-thread", "--c-host", "--c-job", "--c-run"];
const hash = s => { let n = 0; for(const c of String(s)) n = (n * 31 + c.charCodeAt(0)) >>> 0; return n; };
function homeShelves(v, f){
  let h = '<section class="nb-folders nbx-shelves block" aria-labelledby="nb-folders-title">';
  if(!f) return h + sectionHead("nb-folders-title", "Shelves") + '<p class="nb-inline-error" role="status">' + esc(S.foldersErr || 'Folders could not be loaded.') + '</p></section>';
  if(!f.folders.length) return h + sectionHead("nb-folders-title", "Shelves") + '<p class="nb-quiet">No folders with pages yet.</p></section>';
  const gmap = new Map((v.graph && v.graph.folders || []).map(g => [g.folder, g]));
  let tops = f.folders.filter(fo => !fo.folder.includes("/"));
  if(gmap.size){
    const rank = fo => !fo.folder ? 3 : COLD.test(fo.folder) ? 2 : (gmap.has(fo.folder) ? 0 : 1);
    tops = tops.map((fo, i) => ({fo, i})).sort((a, b) => rank(a.fo) - rank(b.fo) ||
      ((gmap.get(b.fo.folder) || {}).last_mtime || 0) - ((gmap.get(a.fo.folder) || {}).last_mtime || 0) || a.i - b.i).map(x => x.fo);
  }
  const max = Math.max(1, ...tops.map(fo => fo.count));
  h += sectionHead("nb-folders-title", "Shelves", "The rest of the library, " + plural(tops.length, "shelf", "shelves") + ". Taller means more pages." + (gmap.size ? (touchy() ? " Tap a spine to open it." : " Hover a spine for its newest page.") : ""));
  h += '<div class="nbx-shelf nb-folder-list">';
  for(const fo of tops){
    const g = gmap.get(fo.folder);
    const ht = Math.round(96 + 56 * (Math.log(fo.count + 1) / Math.log(max + 1)));
    const col = SPINE_COLORS[hash(fo.folder) % SPINE_COLORS.length];
    const wk = g && Array.isArray(g.week) ? g.week : null, wmax = wk ? Math.max(1, ...wk) : 1;
    h += '<a class="nbx-spine' + (COLD.test(fo.folder) ? ' cold' : '') + '" href="' + folderHref(fo.folder) + '" style="--sc:var(' + col + ');--h:' + ht + 'px">' +
         '<b>' + esc(fo.folder || 'Root pages') + '</b>' +
         (wk ? '<span class="wk" aria-hidden="true" title="pages touched in the last 7 days">' + wk.map(n => '<i style="height:' + Math.max(n ? 3 : 1, Math.round(16 * n / wmax)) + 'px"></i>').join('') + '</span>' : '') +
         '<span class="ct">' + esc(fo.count) + '<span class="nbx-pg">' + (fo.count === 1 ? ' page' : ' pages') + '</span></span>' +
         (g && g.last_title ? '<span class="nw">Newest: ' + esc(g.last_title) + ', ' + esc(age(g.last_mtime)) + '</span>' : '') + '</a>';
  }
  return h + '</div></section>';
}

// Most linked and open checkboxes: two quiet columns of the pages everything points at, and the lists with boxes to tick.
function homeLinked(v){
  const g = v.graph;
  const items = g && Array.isArray(g.most_linked) ? g.most_linked.slice(0, 8) : [];
  if(!items.length) return '';
  const max = Math.max(1, ...items.map(i => i.links_in));
  let h = '<section class="nbx-linked" aria-labelledby="nb-linked-title">' + sectionHead("nb-linked-title", "Most linked", "The pages everything else points at.") + '<ul class="ml">';
  for(const i of items) h += '<li>' + noteChip(i.rel, i.title) + '<div class="bar"><i style="width:' + Math.round(100 * i.links_in / max) + '%"></i></div><span class="n" title="' + esc(plural(i.links_in, 'note links', 'notes link')) + ' here">' + esc(i.links_in) + '</span></li>';
  h += '</ul>';
  if(g.unresolved_links) h += '<p class="nbx-quiet muted">' + esc(plural(g.unresolved_links, "link points", "links point")) + ' at a page that does not exist yet.</p>';
  return h + '</section>';
}
function homeBoxes(v){
  const t = v.tasks;
  const notes = t && Array.isArray(t.notes) ? t.notes.filter(n => n.open > 0).slice(0, 6) : [];
  if(!notes.length) return '';
  let h = '<section class="nbx-boxes" aria-labelledby="nb-boxes-title">' + sectionHead("nb-boxes-title", "Open checkboxes", t.total_open != null ? esc(plural(t.total_open, "open box", "open boxes")) + " across the notebook." : "Lists with something left to tick.") + '<ul class="nbx-boxlist">';
  for(const n of notes){
    h += '<li><div class="nbx-box-h">' + noteChip(n.rel, n.title) + '<span class="hm-pill soft">' + esc(n.open) + ' open</span></div>' +
         (n.items || []).slice(0, 2).map(i => '<a class="nbx-box-i" href="' + noteHref(n.rel) + '"><span class="nb-task-mark" aria-hidden="true"></span><span>' + esc(stripMd(i.text)) + '</span></a>').join('') + '</li>';
  }
  return h + '</ul></section>';
}

// Recently edited: the last twelve pages by modification time, whoever wrote them (the changes feed only sees Helm's own writes).
function homeRecent(v){
  const notes = v.recent && Array.isArray(v.recent.notes) ? v.recent.notes : [];
  let h = '<section class="nb-recent nbx-recent block" aria-labelledby="nb-recent-title">' + sectionHead("nb-recent-title", "Recently edited", "Pick up where the notebook was last touched, by anyone.",
          '<span class="nb-section-meta">' + notes.length + (notes.length === 1 ? ' page' : ' pages') + '</span>');
  if(v.recentErr) return h + '<p class="nb-inline-error" role="status">Recent pages could not be loaded. ' + esc(v.recentErr) + '</p></section>';
  if(!notes.length) return h + '<p class="nb-quiet">No pages yet. Your next note will appear here.</p></section>';
  h += '<ol class="nb-recent-list">';
  for(const n of notes){
    const title = n.title || n.stem || n.rel;
    const folder = n.folder != null ? n.folder : (String(n.rel || '').includes('/') ? String(n.rel).split('/')[0] : '');
    h += '<li><div class="nb-recent-copy"><a class="nb-recent-title" href="' + noteHref(n.rel) + '">' + esc(title) + '</a>' +
         '<a class="nb-recent-folder" href="' + folderHref(folder) + '" aria-label="Open folder ' + esc(folder || 'Root pages') + '">' + esc(folder || 'Root pages') + '</a></div>' +
         modifiedHtml(n.mtime) + '</li>';
  }
  return h + '</ol></section>';
}

// --- the fleet atlas: hosts plotted on a chart, the Docker, API and network pages that link to each hanging off it ---
const ATLAS_CAP = 6, SIDE_PAGES = 6;
function atlasHosts(v){
  const hs = v.atlas && Array.isArray(v.atlas.hosts) ? v.atlas.hosts : [];
  return hs.map(x => Object.assign({}, x, {pages: x.pages || []}))
    .sort((a, b) => b.pages.length - a.pages.length || String(a.title).localeCompare(String(b.title)));
}
function homeAtlas(v){
  if(!isHome() || !v.atlas) return '';
  const all = atlasHosts(v);
  if(!all.length) return '';
  const shown = S.atlasAll ? all : all.slice(0, ATLAS_CAP);
  const pageCount = all.reduce((n, x) => n + x.pages.length, 0);
  let h = '<section class="nbx-atlas block" id="nb-atlas" aria-labelledby="nb-atlas-title"><div class="atlas"><div class="ah"><div>' +
          '<h2 id="nb-atlas-title" class="nbx-h2">Fleet atlas <i>' + esc(plural(all.length, 'host')) + '</i></h2>' +
          '<p class="nbx-sub">Hosts plotted on the chart; the Docker, API and network pages that link to a host hang from it. ' + (touchy() ? 'Tap a host to open it.' : 'Hover a host to peek, click to open.') + '</p></div>' +
          '<div class="legend"><span><svg class="dot host" viewBox="0 0 12 12"><circle cx="6" cy="6" r="4.5"/></svg>Host</span><span><i class="dot-s f-docker"></i>Docker</span><span><i class="dot-s f-apis"></i>APIs</span><span><i class="dot-s f-network"></i>Network</span></div></div>' +
          '<div class="nbx-atlas-box" data-atlas="1"></div>' +
          '<div class="atlas-foot"><span><b>' + all.length + '</b> hosts</span><span><b>' + pageCount + '</b> pages hang from them</span>' +
          (all.length > ATLAS_CAP ? '<button type="button" class="btn quiet sm" data-atlasmore="1">' + (S.atlasAll ? 'Show the busiest ' + ATLAS_CAP : 'Show all ' + all.length + ' hosts') + '</button>' : '') + '</div></div></section>';
  return h;
}
const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, Math.max(1, n - 1)).trimEnd() + "…" : s; };
const folderClass = f => "f-" + String(f || "").toLowerCase().replace(/[^a-z]/g, "");
function atlasChart(hosts, W){
  const cols = W >= 1180 ? 3 : 2, m = 34;
  const cell = (W - m - 6) / cols, sideMax = Math.floor((cell / 2 - 64) / 6.7);
  const rows = []; for(let i = 0; i < hosts.length; i += cols) rows.push(hosts.slice(i, i + cols));
  let y = m, o = "", labels = "";
  const pos = [];
  rows.forEach((row, ri) => {
    const spread = Math.max(...row.map(hh => Math.ceil(Math.min(hh.pages.length, SIDE_PAGES) / 2))) * 26;
    const cy = y + 22 + Math.max(spread / 2, 18);
    const rowH = (cy - y) + Math.max(58, spread / 2 + 12) + 22;
    row.forEach((hh, ci) => pos.push({hh, cx: m + cell * ci + cell / 2, cy}));
    labels += '<text class="axis" x="14" y="' + (cy + 4) + '" text-anchor="middle">' + (ri + 1) + '</text>';
    y += rowH;
  });
  const H_ = Math.round(y + 8);
  for(let i = 0; i < cols; i++) labels += '<text class="axis" x="' + (m + cell * i + cell / 2) + '" y="20" text-anchor="middle">' + String.fromCharCode(65 + i) + '</text>';
  for(let x = m; x <= W - 6; x += 20) o += '<line class="grid' + (((x - m) % 100 === 0) ? ' maj' : '') + '" x1="' + x + '" y1="' + m + '" x2="' + x + '" y2="' + (H_ - 6) + '"/>';
  for(let yy = m; yy <= H_ - 6; yy += 20) o += '<line class="grid' + (((yy - m) % 100 === 0) ? ' maj' : '') + '" x1="' + m + '" y1="' + yy + '" x2="' + (W - 6) + '" y2="' + yy + '"/>';
  for(const {hh, cx, cy} of pos){
    const pg = hh.pages.slice(0, SIDE_PAGES);
    const right = pg.filter((_, i) => i % 2 === 0), left = pg.filter((_, i) => i % 2 === 1);
    const place = (arr, sign) => arr.forEach((p, k) => {
      const py = cy + (k - (arr.length - 1) / 2) * 26, px = cx + sign * 62;
      o += '<line class="leader" x1="' + cx + '" y1="' + cy + '" x2="' + px + '" y2="' + py + '"/>' +
           '<a href="' + noteHref(p.rel) + '" class="pgl ' + folderClass(p.folder) + '" tabindex="0" data-peek="note" data-peek-id="' + esc(p.rel) + '" data-peek-label="' + esc(p.title) + '">' +
           '<circle cx="' + px + '" cy="' + py + '" r="4.5"/><text class="pg" x="' + (px + sign * 10) + '" y="' + (py + 5) + '" text-anchor="' + (sign > 0 ? 'start' : 'end') + '">' + esc(trunc(p.title, sideMax)) + '</text></a>';
    });
    place(right, 1); place(left, -1);
    o += '<a href="' + noteHref(hh.rel) + '" class="hostg" tabindex="0" data-peek="host" data-peek-id="' + esc(hh.rel) + '" data-peek-label="' + esc(hh.title) + '">' +
         '<circle class="hc" cx="' + cx + '" cy="' + cy + '" r="13"/><circle class="hd" cx="' + cx + '" cy="' + cy + '" r="4.5"/>' +
         '<text class="hn" x="' + cx + '" y="' + (cy + 34) + '" text-anchor="middle">' + esc(trunc(hh.title, Math.floor(cell / 9))) + '</text></a>' +
         '<text class="hs" x="' + cx + '" y="' + (cy + 49) + '" text-anchor="middle">' + esc(plural(hh.pages.length, 'page') + (hh.pages.length > SIDE_PAGES ? ', ' + SIDE_PAGES + ' shown' : '')) + '</text>';
  }
  return '<svg class="chart" role="img" aria-label="Chart of hosts and the pages that link to them" viewBox="0 0 ' + W + ' ' + H_ + '" width="' + W + '" height="' + H_ + '">' + o + labels + '</svg>';
}
function atlasList(hosts){
  return '<ul class="nbx-atlas-list">' + hosts.map(hh => '<li>' + chipHtml({type: "host", id: hh.rel, label: hh.title, href: noteHref(hh.rel)}) +
    '<div class="why">' + hh.pages.slice(0, 8).map(p => noteChip(p.rel, p.title)).join(' ') + (hh.pages.length > 8 ? ' <span class="muted">+' + (hh.pages.length - 8) + '</span>' : '') + '</div></li>').join('') + '</ul>';
}
let atlasRO = null;
function drawAtlas(){
  const box = main() && main().querySelector('[data-atlas]');
  if(!box || !S.view || !S.view.home) return;
  const hosts = S.atlasAll ? atlasHosts(S.view) : atlasHosts(S.view).slice(0, ATLAS_CAP);
  const W = Math.floor(box.clientWidth);
  if(!W){ requestAnimationFrame(drawAtlas); return; }
  const mode = (W < 560 ? 'l' : W >= 1180 ? 'c3' : 'c2') + S.atlasAll;
  if(box.dataset.mode === mode && box.dataset.w === String(W)) return;
  box.dataset.mode = mode; box.dataset.w = String(W);
  box.innerHTML = W < 560 ? atlasList(hosts) : atlasChart(hosts, W);
}
window.addEventListener("resize", () => { clearTimeout(atlasRO); atlasRO = setTimeout(drawAtlas, 120); });

function folderHomeView(){
  const f = S.folders;
  const v = S.view && S.view.home ? S.view : {recent: null, recentErr: S.viewErr, day: null, dayErr: S.viewErr};
  const metaStats = S.meta && S.meta.notes && typeof S.meta.notes === 'object' ? S.meta.notes : {};
  const stats = f && f.stats ? f.stats : metaStats;
  const noteCount = stats.notes != null ? stats.notes : (S.meta && typeof S.meta.notes === 'number' ? S.meta.notes : null);
  const changed = changedGroups(v).length;
  const play = inPlay(v.tasks).length;
  const deck = [noteCount != null ? '<b>' + esc(noteCount.toLocaleString()) + ' pages</b>' : '',
                changed ? esc(changed) + ' changed today' : '', play ? esc(play) + ' with something open' : ''].filter(Boolean);
  let h = '<div class="view nb-home"><header class="nb-home-head"><div class="nb-home-intro">' +
          '<h1>The notebook</h1><p class="nb-home-deck">' + (deck.length ? deck.join(', ') + '. Start with today.' : 'Start with today.') + '</p></div>' +
          '<button type="button" class="hm-input nbx-search" data-pal="1" aria-label="' + (hasPalette() ? 'Find a page, a tag or a sentence' : 'Find a note') + '">' + glyphHtml("search", 18) + '<span>' + (hasPalette() ? 'Find a page, a tag, or a sentence' : 'Find a note by its name') + '</span>' + (hasPalette() ? '<kbd>Ctrl K</kbd>' : '<kbd>Ctrl O</kbd>') + '</button>' +
          '<div class="nb-home-actions">' + (readOnly() && !(v.day && v.day.exists) ? '' : '<a class="nb-primary-action btn primary" href="' + dayHref(localDate(new Date())) + '">Open today</a>') +
          (readOnly() ? '' : '<button type="button" class="nb-secondary-action btn secondary" data-newnote="">New note</button>') + '</div></header>';
  h += '<dl class="nb-stats" aria-label="Notebook contents">' + homeStat('Notes', noteCount) +
       homeStat('Files', stats.files) + homeStat('Resolved links', stats.links_resolved, stats.links == null ? '' : 'of ' + stats.links + ' total') + '</dl>';
  h += '<p class="nb-home-mode">' + (readOnly()
       ? 'Read-only here: search, open pages and follow their connections into the work around them.'
       : 'Open a note and press Ctrl+E to edit it; Alt+N starts a new one in the folder you are in.') + '</p>';
  h += '<div class="nb-home-grid">' + homeToday(v, f) + homeChanged(v) + '</div>' + homePlay(v) + homeAtlas(v) + homeShelves(v, f);
  const linked = homeLinked(v), boxes = homeBoxes(v);
  if(linked || boxes) h += '<div class="nbx-two block">' + linked + boxes + '</div>';
  return h + homeRecent(v) + '</div>';
}

// --- Rendering: a folder ---------------------------------------------------------------------------
function folderView(){
  const v = S.view;
  if(!v) return emptyBlock(S.viewErr || "loading");
  const isDaily = v.folder === "Daily" || /^Daily\/\d{4}/.test(v.folder);
  const sort = lsGet("nb.sort", "updated") === "name" ? "name" : "updated";
  const label = v.folder || 'Root pages';
  const subs = ((S.folders && S.folders.folders) || []).filter(fo => v.folder && fo.folder.startsWith(v.folder + "/") && !fo.folder.slice(v.folder.length + 1).includes("/"));
  let h = '<div class="view nb-folder-view' + (v.folder ? '' : ' nb-root-folder') + '" data-folder="' + esc(v.folder) + '">' +
          '<div class="crumb hm-crumb"><a href="#/notebook">Notebook</a><span>/</span><span>' + esc(label) + '</span> <span class="muted">' + plural(v.notes.length, 'page') + '</span>' +
          (!v.folder || v.folder.startsWith("Daily") || readOnly() ? '' : '<span class="ntools"><button class="quiet btn sm" data-newnote="' + esc(v.folder) + '" title="Alt+N">New note</button></span>') + '</div>' +
          '<h1 class="nbx-title nbx-folder-title">' + esc(label.split("/").pop()) + '</h1>';
  if(subs.length){
    h += '<div class="nbx-shelf nbx-subshelf">' + subs.map(fo => {
      const ht = Math.round(78 + 26 * Math.min(1, Math.log(fo.count + 1) / Math.log(60)));
      return '<a class="nbx-spine' + (COLD.test(fo.folder) ? ' cold' : '') + '" href="' + folderHref(fo.folder) + '" style="--sc:var(' + SPINE_COLORS[hash(fo.folder) % SPINE_COLORS.length] + ');--h:' + ht + 'px">' +
             '<b>' + esc(fo.folder.split("/").pop()) + '</b><span class="ct">' + esc(fo.count) + '<span class="nbx-pg">' + (fo.count === 1 ? ' page' : ' pages') + '</span></span></a>';
    }).join('') + '</div>';
  }
  if(!v.notes.length) return h + '<p class="nb-quiet">No pages in this folder.</p></div>';
  if(isDaily){
    let month = null;
    h += '<ul class="flist">';
    for(const n of v.notes){
      const m = n.stem.slice(0, 7);
      if(m !== month){ month = m; h += '</ul><div class="month">' + esc(monthName(m)) + '</div><ul class="flist">'; }
      const wd = DATE_RE.test(n.stem) ? new Date(n.stem + 'T12:00:00').toLocaleDateString([], {weekday: 'short'}) : '';
      h += '<li class="nbx-dayrow">' + glyphHtml("day", 15) + '<a href="' + noteHref(n.rel) + '">' + esc(n.stem) + '</a>' + (wd && wd !== 'Invalid Date' ? '<span class="nbx-wd">' + esc(wd) + '</span>' : '') +
           (n.title && n.title !== n.stem ? '<span class="stem">' + esc(n.title) + '</span>' : '') +
           '<span class="when" title="last modified">' + esc(age(n.mtime)) + '</span></li>';
    }
    return h + '</ul></div>';
  }
  const notes = v.notes.slice().sort(sort === "name"
    ? (a, b) => String(a.title || a.stem).localeCompare(String(b.title || b.stem))
    : (a, b) => (b.mtime || 0) - (a.mtime || 0));
  h += '<div class="nbx-fbar"><div class="hm-seg" role="group" aria-label="Sort pages">' +
       '<button type="button" class="hm-seg-b' + (sort === "updated" ? ' on' : '') + '" data-sort="updated" aria-pressed="' + (sort === "updated") + '">Recently updated</button>' +
       '<button type="button" class="hm-seg-b' + (sort === "name" ? ' on' : '') + '" data-sort="name" aria-pressed="' + (sort === "name") + '">Name</button></div></div>';
  h += '<ul class="flist nbx-rows">';
  for(const n of notes){
    const sub = v.folder && n.folder && n.folder.length > v.folder.length ? n.folder.slice(v.folder.length + 1) : '';
    const lin = S.linksIn.get(n.rel);
    h += '<li><a class="nbx-row-t" href="' + noteHref(n.rel) + '">' + esc(n.title || n.stem) + '</a>' +
         (n.title && n.title !== n.stem ? '<span class="stem">' + esc(n.stem) + '</span>' : '') +
         '<span class="nbx-row-m">' + (sub ? '<span class="nbx-sub-f">' + esc(sub) + '</span>' : '') +
         (n.size != null ? '<span>' + esc(kb(n.size)) + '</span>' : '') +
         (lin ? '<span title="notes that link here">' + esc(lin) + ' in</span>' : '') +
         '<span class="when" title="last modified">' + esc(age(n.mtime)) + '</span></span></li>';
  }
  return h + '</ul></div>';
}

// --- Rendering: a note --------------------------------------------------------------------------------
function connectionCount(label, value, href, detail){
  return '<div><dt><a href="' + href + '">' + esc(label) + '</a></dt><dd>' +
         (value == null ? '&mdash;' : esc(value)) +
         (detail ? '<span class="nb-count-note">' + esc(detail) + '</span>' : '') + '</dd></div>';
}
function connectionNotes(items){
  return '<ul class="nb-connection-list">' + items.map(l => {
    const label = l.title || l.alias || l.target || l.rel;
    return '<li><a href="' + noteHref(l.rel) + '">' + esc(label) + '</a>' +
           (l.target && l.target !== label ? '<span>' + esc(l.target) + '</span>' : '') + '</li>';
  }).join('') + '</ul>';
}
function connectionFiles(items){
  return '<ul class="nb-connection-list">' + items.map(l => '<li><a href="' + esc(fileHref(l.rel)) + '">' +
         esc(l.alias || l.target || l.rel) + '</a><span>' + esc(l.rel) + '</span></li>').join('') + '</ul>';
}
// What a note links to, sorted: pages (once each), files, and names no note has.
function outlinkGroups(n){
  const notes = [], files = [], missing = [];
  const seenNotes = new Set(), seenFiles = new Set(), seenMissing = new Set();
  for(const l of n.outlinks || []){
    const rel = String(l.rel || '');
    const target = String(l.target || '');
    if(rel && /\.md$/i.test(rel)){
      const key = rel.toLowerCase();
      if(!seenNotes.has(key)){ seenNotes.add(key); notes.push(l); }
    }else if(rel){
      const key = rel.toLowerCase();
      if(!seenFiles.has(key)){ seenFiles.add(key); files.push(l); }
    }else{
      const key = target.toLowerCase();
      if(target && !seenMissing.has(key)){ seenMissing.add(key); missing.push(l); }
    }
  }
  return {notes, files, missing};
}
function ctxOf(v){ const c = v && v.ctx; return c && !c.error ? c : null; }
function connectionsHtml(n, c){
  const {notes, files, missing} = outlinkGroups(n);
  const ctxReady = !!(c && !c.error);
  const backlinks = Array.isArray(n.backlinks) ? n.backlinks : (ctxReady && Array.isArray(c.backlinks) ? c.backlinks : []);
  const backlinkCount = new Set(backlinks.map(b => String(b.rel || '').toLowerCase()).filter(Boolean)).size;
  const open = ctxReady && Array.isArray(c.open) ? c.open : [];
  const settled = ctxReady && Array.isArray(c.settled) ? c.settled : [];
  const settledTotal = ctxReady ? (c.settled_total == null ? settled.length : c.settled_total) : null;
  const dailyCount = ctxReady && Array.isArray(c.daily_mentions) ? c.daily_mentions.length : null;
  const openSuggested = open.filter(t => t.match === 'suggested').length;
  const openDetail = openSuggested ? (open.length - openSuggested) + ' explicit · ' + openSuggested + ' suggested' : '';
  const settledSuggested = settled.filter(t => t.match === 'suggested').length;
  const settledDetail = settledSuggested && settledTotal === settled.length
    ? (settled.length - settledSuggested) + ' explicit · ' + settledSuggested + ' suggested' : '';
  const topic = topicHref(n.rel);
  let h = '<section class="nb-connections" aria-labelledby="nb-connections-title"><header class="nb-connections-head">' +
          '<h2 id="nb-connections-title">Around this note</h2>' +
          '<a class="nb-text-link" href="' + topic + '" aria-label="Open the full topic for ' + esc(n.title || n.stem) + '">Open full topic</a></header>';
  h += '<dl class="nb-connection-stats">' + connectionCount('Backlinks', backlinkCount, topic) +
       connectionCount('Open threads', ctxReady ? open.length : null, topic, openDetail) +
       connectionCount('Settled threads', settledTotal, topic, settledDetail) +
       connectionCount('Daily mentions', dailyCount, topic) + '</dl>';
  if(c && c.error) h += '<p class="nb-inline-error" role="status">Thread context could not be loaded. ' + esc(c.error) + '</p>';
  if(openDetail || settledDetail) h += '<p class="nb-match-note">Explicit threads link this note. Suggested threads match its topic.</p>';
  // On a phone there is no right pane: the Connected content is drawn here instead (render() fills this in).
  h += '<div class="nbx-conn-inline" id="nb-conn-inline"></div>';
  // The thread cards, answerable where the note is read: reply, redirect, snooze, backlog, close, mute, park, reopen, history.
  if(ctxReady && (open.length || settledTotal)) h += '<div class="nbx-note-threads" id="nb-note-threads">' + topicHtml(c, n.rel, true) + '</div>';
  if(notes.length || files.length){
    h += '<div class="nb-connection-columns">';
    if(notes.length) h += '<section><h3>Outgoing notes <span>' + notes.length + '</span></h3>' + connectionNotes(notes) + '</section>';
    if(files.length) h += '<section><h3>Files <span>' + files.length + '</span></h3>' + connectionFiles(files) + '</section>';
    h += '</div>';
  }else if(!missing.length) h += '<p class="nb-quiet nbx-nolinks">No outgoing wikilinks in this note.</p>';
  if(missing.length){
    h += '<section class="nb-unresolved"><h3>Unresolved links <span>' + missing.length + '</span></h3><ul>' +
         missing.map(l => '<li><span>' + esc(l.alias || l.target) + '</span><small>no matching note</small></li>').join('') + '</ul></section>';
  }
  return h + '</section>';
}

// The note's rendered page, enhanced: headings get ids (the outline), wikilinks and board card ids become chips, and
// the title, the at-a-glance line, the pills and the latest entries go in after the first heading.
function glanceHtml(n, c, x){
  const bits = [];
  const rev = (x && x.history || []).slice().sort((a, b) => (b.ts || 0) - (a.ts || 0))[0];
  const p = whenParts(n.mtime);
  const by = rev && rev.source ? ' by ' + esc(rev.source) : '';
  bits.push('<span>Updated&nbsp;' + esc(p.day.toLowerCase() === 'today' ? 'today' : p.day) + '&nbsp;' + esc(p.time).replace(/ /g, '&nbsp;') + by + '</span>');
  bits.push('<span>' + esc(kb(n.size)) + ',&nbsp;' + readMins(n.size) + '&nbsp;min&nbsp;read</span>');
  const ctxReady = !!(c && !c.error);
  const links = Array.isArray(n.backlinks) ? n.backlinks : (ctxReady && Array.isArray(c.backlinks) ? c.backlinks : []);
  const nIn = new Set(links.map(b => String(b.rel || '').toLowerCase()).filter(Boolean)).size;
  bits.push('<span>' + nIn + '&nbsp;' + (nIn === 1 ? 'link' : 'links') + '&nbsp;in</span>');
  const openTasks = (n.tasks || []).filter(t => !t.done).length;
  if(openTasks) bits.push('<b class="hot">' + openTasks + '&nbsp;open&nbsp;' + (openTasks === 1 ? 'task' : 'tasks') + '</b>');
  const waiting = ctxReady ? (c.open || []).filter(t => t.state === 'needs-you').length : 0;
  if(waiting) bits.push('<b class="hot">' + waiting + '&nbsp;waiting&nbsp;' + (waiting === 1 ? 'thread' : 'threads') + '</b>');
  let h = '<p class="gline nbx-glance">' + bits.join('') + '</p>';
  // tags and a few frontmatter keys as quiet pills; the raw grid sits behind a disclosure
  const fm = n.frontmatter || {};
  const pills = [];
  const asList = v => (Array.isArray(v) ? v : String(v == null ? '' : v).split(/[,\s]+/)).map(s => String(s).replace(/^#/, '').trim()).filter(Boolean);
  for(const k of Object.keys(fm)){
    if(/^tags?$/i.test(k)) asList(fm[k]).forEach(t => pills.push('<span class="hm-pill soft nbx-tag" title="tag">' + esc(t) + '</span>'));
    else if(/^(status|state|type|kind|stage)$/i.test(k) && fm[k] != null && fm[k] !== '') pills.push('<span class="hm-pill ' + (/^(live|done|complete|shipped)$/i.test(fm[k]) ? 'live' : 'soft') + '" title="' + esc(k) + '">' + esc(fm[k]) + '</span>');
  }
  if(pills.length) h += '<div class="nt-tags nbx-pills">' + pills.join('') + '</div>';
  if(Object.keys(fm).length) h += '<details class="nbx-props"><summary><span class="nbx-caret" aria-hidden="true"></span>All properties</summary>' + propsHtml(fm) + '</details>';
  return h;
}
function latestHtml(n, x, headIds){
  const t = x && x.tail;
  if(!t || !Array.isArray(t.lines) || !t.lines.length) return '';
  const lines = t.lines.slice().reverse();
  const older = t.last_heading ? headIds.get(stripMd(t.last_heading.text)) : null;
  let h = '<section class="nbx-latest" id="sec-latest"><div class="lh"><h2>Latest entries</h2>' +
          (older ? '<a href="' + noteHref(n.rel) + '" data-nbsec="' + older + '">Older entries in ' + esc(stripMd(t.last_heading.text)) + '</a>' : '') + '</div>';
  for(const l of lines){
    const st = lineStamp(stripMd(l.text));
    let time = '';
    if(st){
      const iso = st.date, today = localDate(new Date());
      const day = iso === today ? 'Today' : shortDateBrief(iso);
      time = '<time>' + esc(day) + (st.time ? '<small>' + esc(st.time) + '</small>' : '') + '</time>';
    }else time = '<time></time>';
    h += '<div class="le">' + time + '<p>' + lineHtml(st ? st.rest : l.text) + '</p></div>';
  }
  return h + '</section>';
}
function shortDateBrief(iso){ const d = new Date(iso + "T12:00:00"); return isNaN(d) ? iso : d.toLocaleDateString([], {day: "numeric", month: "short"}); }
// A very large page (the ops log is megabytes) is drawn in pieces: the first screens now, the rest as the reader gets near
// the end of what is drawn (or asks for it). Cut only at a heading and only where no list, table or quote is left open.
const LAZY_MIN = 400000, LAZY_CHUNK = 140000;
const OPEN_TAG = /<(ul|ol|blockquote|table|details|pre|div)[\s>]/g, CLOSE_TAG = /<\/(ul|ol|blockquote|table|details|pre|div)>/g;
const balanced = h => (h.match(OPEN_TAG) || []).length === (h.match(CLOSE_TAG) || []).length;
function splitHtml(html){
  if(html.length <= LAZY_MIN) return [html];
  const parts = html.split(/(?=<h[1-4][\s>])/);   // a literal <h2 in rendered html is a heading: code is escaped
  if(parts.length < 2) return [html];
  const chunks = []; let cur = "";
  for(const p of parts){
    cur += p;
    if(cur.length >= LAZY_CHUNK && balanced(cur)){ chunks.push(cur); cur = ""; }
  }
  if(cur) chunks.push(cur);
  return chunks;
}
// Headings of a piece that is not drawn yet: the same h1-h4 order the draw will give them ids in.
const decodeText = t => { const d = document.createElement("textarea"); d.innerHTML = t; return d.value; };
function scanHeads(html, start){
  const out = []; const re = /<h([1-4])[^>]*>([\s\S]*?)<\/h\1>/gi; let m, i = start;
  while((m = re.exec(html))){ out.push({id: 'sec-' + i, level: +m[1], text: decodeText(m[2].replace(/<[^>]*>/g, "")).trim()}); i++; }
  return out;
}
// The outline lists sections, not log lines: a heading that is a dated entry or an all-caps instruction is left out, and a very
// long page keeps only its top-level sections.
const DATED_HEAD = /^\d{4}-\d{2}-\d{2}\b/, SHOUT = /^[^a-z]*[A-Z][^a-z]*$/;
const OUTLINE_MAX = 40;
function tidyOutline(list){
  let out = list.filter(o => o.text && !DATED_HEAD.test(o.text) && !(o.text.length > 12 && SHOUT.test(o.text)));
  if(out.length > OUTLINE_MAX) out = out.filter(o => o.level <= 2);
  return out.slice(0, OUTLINE_MAX);
}
let LAZY = null;
function enhanceChunk(html, start, x){
  const doc = new DOMParser().parseFromString('<div id="r">' + (html || '') + '</div>', 'text/html');
  const root = doc.getElementById('r');
  const heads = [];
  root.querySelectorAll('h1,h2,h3,h4').forEach((hd, i) => {
    hd.id = 'sec-' + (start + i);
    heads.push({id: hd.id, level: +hd.tagName[1], text: hd.textContent.trim()});
  });
  // wikilinks become chips of their type
  root.querySelectorAll('a.wl').forEach(a => {
    if(a.classList.contains('file')) return;
    const m = /^#\/note\/(.+)$/.exec(a.getAttribute('href') || '');
    if(!m) return;
    let rel; try{ rel = decodeURIComponent(m[1]); }catch(e){ return; }
    const t = doc.createElement('span');
    t.innerHTML = noteChip(rel, a.textContent);
    if(t.firstElementChild) a.replaceWith(t.firstElementChild);
  });
  return {root, heads};
}
function enhanceNote(n, c, x){
  const chunks = splitHtml(n.html || '');
  const first = enhanceChunk(chunks[0], 0, x), root = first.root;
  let heads = first.heads;
  let idx = heads.length;
  LAZY = null;
  if(chunks.length > 1){
    for(let k = 1; k < chunks.length; k++){ const h = scanHeads(chunks[k], idx); heads = heads.concat(h); idx += h.length; }
    LAZY = {rel: n.rel, chunks, next: 1, idx: first.heads.length, x};
  }
  const headIds = new Map();
  for(const h of heads) headIds.set(h.text, h.id);
  const outline = tidyOutline(heads.filter(h => h.level !== 1));
  const lead = glanceHtml(n, c, x) + latestHtml(n, x, headIds);
  const firstEl = root.firstElementChild;
  if(firstEl && firstEl.tagName === 'H1') firstEl.insertAdjacentHTML('afterend', lead);
  else root.insertAdjacentHTML('afterbegin', '<h1 class="nbx-title">' + esc(n.title || n.stem) + '</h1>' + lead);
  if(lead.includes('id="sec-latest"')) outline.unshift({id: 'sec-latest', text: 'Latest entries', level: 2});
  if(LAZY) root.insertAdjacentHTML('beforeend', '<div class="nbx-more" id="nbx-more"><button class="btn sm" type="button" data-nbmore="1">Show older entries</button></div>');
  return {html: root.innerHTML, outline};
}
// Draw the next piece (or pieces, up to a heading that is not drawn yet) of a large page.
function mountMore(untilId){
  const L = LAZY, md = main() && main().querySelector('.md'), hold = $("nbx-more");
  if(!L || !md || !hold || L.rel !== (S.route && S.route.rel)) return false;
  do{
    const e = enhanceChunk(L.chunks[L.next], L.idx, L.x);
    L.idx += e.heads.length; L.next++;
    hold.insertAdjacentHTML('beforebegin', e.root.innerHTML);
  }while(L.next < L.chunks.length && untilId && !$(untilId));
  const editable = !!(S.view && S.view.note && S.view.note.editable);
  md.querySelectorAll(".task-box").forEach(cb => { cb.disabled = !editable; cb.title = editable ? "tick or untick this line in the note" : ((S.view && S.view.note && S.view.note.reason) || ""); });
  if(L.next >= L.chunks.length){ hold.remove(); if(L.io){ L.io.disconnect(); L.io = null; } LAZY = null; }
  return true;
}
// After a draw: draw more of a large page when the reader gets near the end of what is there.
function lazyWatch(){
  const hold = $("nbx-more"), sc = $("view-notebook");
  if(!hold || !LAZY || !sc) return;
  if(LAZY.io) LAZY.io.disconnect();
  const near = () => { const r = hold.getBoundingClientRect(); return r.top < window.innerHeight + 1400; };
  const pump = () => { for(let i = 0; i < 4 && LAZY && $("nbx-more") && near(); i++) if(!mountMore()) break; };
  if(typeof IntersectionObserver === "function"){
    LAZY.io = new IntersectionObserver(es => { if(es.some(e => e.isIntersecting)) pump(); }, {root: sc, rootMargin: "1400px 0px"});
    LAZY.io.observe(hold);
  }
}
function outlineHtml(outline, n){
  if(outline.length < 2) return '';
  return '<aside class="nbx-outline" aria-label="On this page"><div class="in"><h4>On this page</h4><ol>' +
    outline.map(o => '<li class="l' + o.level + '"><a href="' + noteHref(n.rel) + '" data-nbsec="' + esc(o.id) + '">' + esc(o.text) + '</a></li>').join('') + '</ol></div></aside>';
}

function noteView(){
  const v = S.view;
  if(!v && S.gone === "note:" + S.route.rel) return missingView(S.route.rel);
  if(!v) return emptyBlock(S.viewErr || "loading");
  const n = v.note;
  const fo = folderOf(n.rel);
  const editing = !!(S.editing && v.raw && v.raw.editable);
  let tools = '<span class="ntools">';
  if(editing) tools += '<span class="estatus" id="estatus"></span><button class="quiet btn sm" data-go="' + noteHref(n.rel) + '" title="back to reading (Ctrl+E)">Read</button>';
  else if(readOnly()) tools += '<span class="muted" title="read-only at this site: notes are edited in the live ' + appName() + ' notebook">Read-only</span>';
  else if(n.editable) tools += '<button class="quiet btn sm" data-go="' + editHref(n.rel) + '" title="Ctrl+E">Edit</button>';
  else tools += '<span class="muted" title="' + esc(n.reason || "") + '">History: appends only</span>';
  if(!n.daily && !readOnly()) tools += '<button class="quiet btn sm" data-rename="' + esc(n.rel) + '" title="rename, rewriting every link to it">Rename</button>';
  tools += '<a class="quiet btn sm" href="' + topicHref(n.rel) + '" title="the threads, backlinks and daily notes around this note">Topic</a></span>';
  const folderCrumb = fo ? '<a href="' + folderHref(fo) + '">' + esc(fo) + '</a><span>/</span>' : '';
  const crumb = '<div class="crumb hm-crumb"><a href="#/notebook">Notebook</a><span>/</span>' + folderCrumb + '<span class="nbx-cur">' + esc(n.stem) + '</span>' +
          (n.daily ? '<span>&middot;</span><a href="' + dayHref(n.stem) + '">Open as a day</a>' : '') + tools + '</div>';
  if(editing){
    return '<div class="view nb-note-view nbx-editing">' + crumb + '<div class="note editing">' + propsHtml(n.frontmatter) + '<div id="ebar" class="ebar hidden"></div><div id="editor-host"></div></div></div>';
  }
  const en = enhanceNote(n, ctxOf(v) || v.ctx, v.x);
  return '<div class="view nb-note-view">' + outlineHtml(en.outline, n) + '<div class="nbx-note-main">' + crumb +
         '<div class="note"><div class="md">' + (en.html || '<p class="muted">empty</p>') + '</div></div>' + connectionsHtml(n, v.ctx) + '</div></div>';
}

// --- The Connected pane: what points at this note, and what this note points at ----------------------------
const THREAD_STATE = {"needs-you": "Waiting on you", working: "In flight", "waiting-external": "On hold", backlog: "Backlog", done: "Done"};
const clip = (s, n) => { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
function snippetHtml(text, n){
  const t = String(text || "").replace(/^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|#{1,6}\s+|>\s+)/, "").trim();
  if(!t) return "";
  const stem = stemOf(n.rel).toLowerCase(), relNo = String(n.rel).replace(/\.md$/i, "").toLowerCase();
  const re = /\[\[([^\]|#\n]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g;
  // Cut the plain text first, then escape each piece: the cut can never land inside an entity or an <em>.
  const pieces = []; let last = 0, m;
  while((m = re.exec(t))){
    if(m.index > last) pieces.push({text: t.slice(last, m.index)});
    const target = m[1].trim().toLowerCase(), label = (m[2] || m[1].split("/").pop()).trim();
    pieces.push({text: label, em: target === relNo || target === stem || target.endsWith("/" + stem)});
    last = m.index + m[0].length;
  }
  if(last < t.length) pieces.push({text: t.slice(last)});
  let out = "", left = 360, cut = false;
  for(const p of pieces){
    if(left <= 0){ cut = true; break; }
    let tx = p.text;
    if(tx.length > left){ tx = tx.slice(0, Math.max(0, left - 3)).trimEnd(); cut = true; }
    left -= p.text.length;
    out += p.em ? "<em>" + esc(tx) + "</em>" : esc(tx);
    if(cut) break;
  }
  return cut ? out + "&hellip;" : out;
}
function threadSpec(t){
  return {type: t.state === "needs-you" ? "ask" : "thread", id: t.thread_id, label: t.title || t.thread_id, href: threadHref(t.thread_id), state: THREAD_STATE[t.state] || ""};
}
function askNode(t){
  const asked = (t.catchup && t.catchup.lit_at) || t.last_change;
  const p = whenParts(asked);
  const n = html2el('<div class="nbx-ask">' + threadChip(t) +
    (t.summary || (t.ask && t.ask.prompt) ? '<p class="hm-conn-line">' + esc(clip(t.summary || t.ask.prompt, 150)) + '</p>' : '') +
    '<p class="hm-meta">' + (t.match === "suggested" ? 'Suggested: it matches this note’s topic. ' : '') + (asked ? 'Asked ' + esc(p.day.toLowerCase() === 'today' ? 'today' : p.day) + ' ' + esc(p.time) + '.' : '') + '</p>' +
    '<a class="btn primary sm" href="' + threadHref(t.thread_id) + '">Open it</a></div>');
  return n;
}
// The sections of the Connected pane (and, drawn bigger, of the topic page). skipThreads: the page shows the
// thread cards itself.
function connectedSections(v, opts){
  opts = opts || {};
  const n = v.note, c = ctxOf(v), x = v.x || {};
  const secs = [];
  const open = c ? (c.open || []) : [];
  const asks = open.filter(t => t.state === "needs-you");
  const others = open.filter(t => t.state !== "needs-you");
  const settled = c ? (c.settled || []) : [];
  const settledTotal = c ? (c.settled_total == null ? settled.length : c.settled_total) : 0;
  if(!opts.skipThreads){
    secs.push({id: "waiting", title: "Waiting on you", type: "ask", count: asks.length, items: asks.map(t => ({node: askNode(t)}))});
    const items = others.map(t => ({chip: threadSpec(t), meta: [THREAD_STATE[t.state] || t.state, t.match === "suggested" ? "suggested" : "", age(t.last_change)].filter(Boolean).join(", ")}));
    if(settledTotal){
      const d = html2el('<details class="nbx-settled"><summary>' + esc(settledTotal) + ' settled</summary><div class="nbx-chips">' + settled.map(threadChip).join(' ') +
                        (settledTotal > settled.length ? ' <span class="muted">and ' + (settledTotal - settled.length) + ' older</span>' : '') + '</div></details>');
      items.push({node: d});
    }
    secs.push({id: "threads", title: "Threads about it", type: "thread", count: others.length + settledTotal, items});
  }
  const backs = Array.isArray(n.backlinks) ? n.backlinks : (c && Array.isArray(c.backlinks) ? c.backlinks : []);
  secs.push({id: "linked", title: "Linked from", type: "note", count: backs.length ? plural(new Set(backs.map(b => b.rel)).size, "page") : 0,
    items: backs.slice(0, 40).map(b => {
      const dayStem = DATE_RE.test(stemOf(b.rel));
      const it = {chip: {type: dayStem ? "day" : "note", id: dayStem ? stemOf(b.rel) : b.rel, label: b.title || stemOf(b.rel), href: dayStem ? dayHref(stemOf(b.rel)) : noteHref(b.rel)}};
      const sn = snippetHtml(b.text, n);
      if(sn){ const p = document.createElement("p"); p.className = "hm-conn-line"; p.innerHTML = "“" + sn + "”"; it.line = p; }
      return it;
    })});
  const {notes, files} = outlinkGroups(n);
  if(notes.length || files.length){
    const d = document.createElement("div"); d.className = "nbx-chips";
    d.innerHTML = notes.slice(0, 24).map(l => noteChip(l.rel, l.title || l.alias || stemOf(l.rel))).join(" ") +
      files.slice(0, 8).map(l => ' <a class="nbx-file" href="' + esc(fileHref(l.rel)) + '">' + esc(l.alias || l.target || l.rel) + '</a>').join("");
    secs.push({id: "out", title: "Links out", type: "note", count: notes.length + files.length, items: [{node: d}]});
  }
  const days = c && Array.isArray(c.daily_mentions) ? c.daily_mentions : [];
  if(days.length){
    const d = document.createElement("div"); d.className = "nbx-chips";
    d.innerHTML = days.slice(0, 30).map(m => dayChip(m.stem)).join(" ");
    secs.push({id: "days", title: "Days it appears on", type: "day", count: days.length, items: [{node: d}]});
  }
  return secs;
}
function connectedCount(secs){
  let n = 0;
  for(const s of secs) if(s.id === "waiting" || s.id === "tasks" || s.id === "out" || s.id === "days" || s.id === "linked" || s.id === "threads") n += (typeof s.count === "number" ? s.count : (s.items ? s.items.length : 0));
  return n;
}

// The topic hub around a note: its threads as full cards (answerable here), then everything else Connected says,
// drawn bigger, as a page of its own at #/topic/<rel>.
function topicHtml(c, rel, noConn){
  if(!c) return null;
  if(c.error) return '<div class="ctx"><section><h3>Threads</h3><div class="small">' + esc(c.error) + '</div></section></div>';
  let h = '<div class="ctx">';
  h += '<section class="nbx-tcards"><h3 class="nbx-h">Threads about this</h3>';
  if(!(c.open || []).length) h += '<div class="small">none open</div>';
  for(const t of c.open || []) h += threadHtml(t, true);
  if(c.settled_total){
    const key = "settled:" + rel;
    const closed = !closedGroups.has(key);   // folded until the key is added, the reverse of a group
    h += '<div class="fold" data-group="' + esc(key) + '">' + (closed ? '&#9656; ' : '&#9662; ') + c.settled_total + ' settled</div>';
    if(!closed) for(const t of c.settled || []) h += threadHtml(t, true);
  }
  if(c.topic && c.keys) h += '<div class="small" style="margin-top:6px">Matched on: ' + esc(c.keys.stem) +
                   ((c.keys.cases || []).length ? ', case ' + esc(c.keys.cases.join(", ")) : '') +
                   (c.keys.name && c.keys.name !== String(c.keys.stem || "").toLowerCase() ? ', "' + esc(c.keys.name) + '"' : '') +
                   '. Suggested means the thread never named this note.</div>';
  h += '</section>' + (noConn ? '' : '<div id="nb-topic-conn" class="nbx-topic-conn"></div>') + '</div>';
  return h;
}
function topicView(){
  const v = S.view;
  if(!v && S.gone === "topic:" + S.route.rel) return missingView(S.route.rel);
  if(!v) return emptyBlock(S.viewErr || "loading");
  const n = v.note, fo = folderOf(n.rel);
  const folderCrumb = fo ? '<a href="' + folderHref(fo) + '">' + esc(fo) + '</a><span>/</span>' : '';
  let h = '<div class="view nb-topic-view"><div class="crumb hm-crumb"><a href="#/notebook">Notebook</a><span>/</span>' + folderCrumb +
          '<a href="' + noteHref(n.rel) + '">' + esc(n.stem) + '</a><span>/</span><span>Topic</span></div>';
  h += '<h1 class="nbx-title nbx-topic-title">' + esc(n.title || n.stem) + '</h1>';
  h += '<p class="hm-lede nbx-lede">Everything around this note: the threads about it, the board tasks, the pages that point here, and where it points. <a href="' + noteHref(n.rel) + '">Read the note</a>.</p>';
  h += (topicHtml(v.ctx, n.rel) || '<div class="empty">no context</div>');
  return h + '</div>';
}

// --- Render ------------------------------------------------------------------------------------------
// While an editor is mounted on the note being shown, the main column is left
// alone: a refresh redraws the rail and the Connected pane only.
function render(){
  const r = S.route;
  const keep = !!(S.editor && r.view === "note" && S.editing && S.editRel === r.rel);
  if(!keep){
    if(S.editor) dropEditor();
    let html;
    if(r.view === "day") html = dayView();
    else if(r.view === "folder") html = r.folder === null ? folderHomeView() : folderView();
    else if(r.view === "note") html = noteView();
    else if(r.view === "topic") html = topicView();
    else html = folderHomeView();
    main().innerHTML = html;
    // A new page starts at its top; a refresh of the same page keeps the place it was read at.
    const drawn = r.view + ":" + (r.rel || r.date || r.folder);
    if(S.drawn !== drawn){ S.drawn = drawn; const sc = $("view-notebook"); if(sc) sc.scrollTop = 0; }
    afterRender();
  }
  $("aside").classList.add("hidden");
  drawConnected(r);
  drawRail();
  if(H && H.setCount && S.meta && S.meta.notes) { const c = S.meta.notes.notes ?? S.meta.notes; H.setCount("notebook", typeof c === "number" && c > 99 ? "99+" : c); }
  if(!keep && r.view === "note" && S.editing && S.view && S.view.raw && S.view.raw.editable) mountNoteEditor();
  if(S.focus){ const t = $(S.focus); if(t){ S.focus = ""; t.scrollIntoView({block: "start"}); } }
}
// The right pane: the Connected sections of the note on screen; nothing on the other pages.
function drawConnected(r){
  if(!H || !H.setProps || !H.route || H.route.view !== "notebook") return;
  const v = S.view;
  if(r.view === "note" && v && v.note){
    // The sections hold DOM nodes, and a node can sit in one place: the pane and the phone's inline copy each get their own.
    const n = connectedCount(connectedSections(v));
    const lede = n ? "What points at this page, and what this page points at." : "Nothing points at this page yet, and no thread is about it.";
    if(H.connected && H.connected.render){
      const node = H.connected.render(connectedSections(v), {lede, count: n});
      node.classList.add("nbx-conn");
      const inline = $("nb-conn-inline");
      if(inline && H.connected.node){ inline.replaceChildren(H.connected.node(connectedSections(v, {skipThreads: true}), {lede})); inline.classList.toggle("empty", !n); }
    }else H.setProps("Connected", null);
  }else{
    H.setProps("", null);
    if(r.view === "topic" && v && v.note && H.connected && H.connected.node){
      const box = $("nb-topic-conn");
      if(box){
        const secs = connectedSections(v, {skipThreads: true});
        box.replaceChildren(H.connected.node(secs, {}));
      }
    }
  }
}
// Rendered checkboxes tick the note where it can be written; elsewhere they
// are shown as they are and cannot be changed.
function afterRender(){
  const r = S.route;
  let editable = false, why = "";
  if(readOnly()){ why = "read-only at this site; tick it in the live " + appName() + " notebook"; }
  else if(r.view === "note" && S.view){ editable = !!S.view.note.editable; why = S.view.note.reason || ""; }
  else if(r.view === "day" && S.view){ editable = !!S.view.today; why = "history: a past day's note accepts only appends"; }
  main().querySelectorAll(".md .task-box").forEach(cb => {
    cb.disabled = !editable;
    cb.title = editable ? "tick or untick this line in the note" : why;
  });
  if(r.view === "folder" && r.folder === null) drawAtlas();
  S.spy = null;
  spy();
  lazyWatch();
}

function dropEditor(){
  if(S.editor) S.editor.destroy();
  S.editor = null; S.editRel = null; S.ebar = null;
}
function rerender(){
  const snap = snapshotInputs([main(), $("aside")]);
  render();
  restoreInputs(snap);
}

// Route change (from the shell): load, then draw.
let navFlight = null, lastNav = null;
export function navigate(route){
  // The shell can ask for the same route twice while the first is still loading (a section registering late re-routes):
  // the second ask joins the first one's answer instead of reading everything again.
  const key = JSON.stringify([route.kind, route.rel, route.date, route.folder, !!route.edit]);
  if(navFlight && navFlight.key === key) return navFlight.p;
  // ...and once more right after it finished (the shell queues the route again): that page is on screen, nothing to read.
  if(lastNav && lastNav.key === key && Date.now() - lastNav.at < 1500 && S.view && !S.refreshing){ render(); return Promise.resolve(); }
  const p = navigateNow(route).then(() => { lastNav = {key, at: Date.now()}; }, e => { lastNav = null; throw e; })
    .finally(() => { if(navFlight && navFlight.p === p) navFlight = null; });
  navFlight = {key, p};
  return p;
}
async function navigateNow(route){
  const next = route.kind === "day" ? {view: "day", date: route.date}
    : route.kind === "note" ? {view: "note", rel: route.rel, edit: !!route.edit && !readOnly()}
    : route.kind === "topic" ? {view: "topic", rel: route.rel}
    : {view: "folder", folder: folderFromRoute(route.folder)};
  if(route.kind === "note" && route.edit && readOnly()) toast("the notebook is read-only at this site (a staging copy or a rollback)");
  if(S.editor && !(next.view === "note" && next.edit && next.rel === S.editRel)){
    const ed = S.editor, rel = S.editRel;
    const ok = await ed.flush();
    if(!ok && ed.isDirty()){ saveDraft(rel, ed.text()); toast("the last edit did not save; it is kept as a draft for when you come back"); }
    dropEditor();
  }
  S.route = next;
  S.focus = next.view === "folder" && next.folder === null && route.params && route.params.get && route.params.get("play") ? "nb-inplay" : "";
  const gen = ++S.gen;
  S.editing = !!next.edit;
  if(!S.folders) await Promise.all([loadFolders(), loadMeta()]);
  await loadView(false, gen);
  if(gen !== S.gen) return;
  S.refreshedAt = S.refreshedAt || Date.now();
  render();
  armTimer();
}
window.addEventListener("beforeunload", ev => {
  if(S.editor && S.editor.isDirty()){ S.editor.save(); ev.preventDefault(); ev.returnValue = ""; }
});

// --- Events -----------------------------------------------------------------------------------------
function wire(){
  if(S.wired) return; S.wired = true;
  // The notebook's own markup lives in three hosts now: the rail (the NOTEBOOK section's list
  // in the left pane), the main view (the middle pane) and the aside (the right pane).
  for(const root of [$("view-notebook"), $("list-notebook"), $("pane-props")]) root.addEventListener("click", async ev => {
    if(ev.target.classList && ev.target.classList.contains("task-box") && ev.target.closest(".md")){ toggleFromRead(ev.target); return; }
    if(ev.target.matches && ev.target.matches("input[data-hometask]")){ toggleHomeTask(ev.target); return; }
    const el = ev.target.closest("[data-act],[data-go],[data-group],[data-append],[data-mkthread],[data-newnote],[data-newday],[data-rename],[data-ebar],[data-nbrefresh],[data-pal],[data-sort],[data-atlasmore],[data-nbsec],[data-nbmore]");
    if(!el) return;
    if(el.dataset.nbrefresh){ refresh(); toast("refreshing"); return; }
    if(el.dataset.pal){ openPalette(); return; }
    if(el.dataset.sort){ lsSet("nb.sort", el.dataset.sort); rerender(); return; }
    if(el.dataset.atlasmore){ S.atlasAll = !S.atlasAll; rerender(); return; }
    if(el.dataset.nbmore){ mountMore(); lazyWatch(); return; }
    if(el.dataset.nbsec){ ev.preventDefault(); let t = $(el.dataset.nbsec); if(!t && LAZY){ mountMore(el.dataset.nbsec); t = $(el.dataset.nbsec); } if(t) t.scrollIntoView({block: "start", behavior: "smooth"}); return; }
    if(el.dataset.newnote !== undefined){ openNewNote(el.dataset.newnote); return; }
    if(el.dataset.newday){ createToday(); return; }
    if(el.dataset.rename){ openRename(el.dataset.rename); return; }
    if(el.dataset.ebar){ ebarAction(el.dataset.ebar); return; }
    if(el.dataset.act){
      if(ev.target.closest("a")) return;   // a link inside a card is a link
      const handled = await cardAction(el, rerender, refresh);
      if(handled) ev.preventDefault();
      return;
    }
    if(el.dataset.go){ H.go(el.dataset.go); return; }
    if(el.dataset.group !== undefined){
      const k = el.dataset.group;
      closedGroups.has(k) ? closedGroups.delete(k) : closedGroups.add(k);
      saveOpen(); rerender(); return;
    }
    if(el.dataset.append){ appendDay(); return; }
    if(el.dataset.mkthread !== undefined){
      // "Make this a thread": the shell's file-a-card sheet, pre-filled.
      if(typeof window.openSheet === "function") window.openSheet({title: el.dataset.mkthread, intent: "mine", link: "note:" + el.dataset.mkrel,
                   summary: "From the open checkbox line in " + el.dataset.mkrel + "."});
      return;
    }
  });

  // Ctrl+Enter inside a card note sends the note as the instruction; inside the
  // append box it appends. The shell owns the letter keys and Ctrl+O / Ctrl+D / Alt+N.
  document.addEventListener("keydown", ev => {
    if(document.body.dataset.view !== "notebook") return;
    if(ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)){
      const a = document.activeElement;
      if(a && a.id && a.id.startsWith("note-")){
        const id = a.id.slice(5);
        const btn = document.querySelector('[data-act="redirect"][data-id="' + CSS.escape(id) + '"]');
        if(btn){ ev.preventDefault(); btn.click(); }
        return;
      }
      if(a && a.id === "append-line"){ ev.preventDefault(); appendDay(); return; }
    }
    if(ev.key === "Escape"){
      if(document.querySelector(".overlay.nb-overlay")){ closeOverlay(); return; }
    }
    if((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && !ev.altKey && (ev.key === "e" || ev.key === "E")){ ev.preventDefault(); toggleEdit(); return; }
    if((ev.ctrlKey || ev.metaKey) && !ev.altKey && (ev.key === "s" || ev.key === "S")){
      const handled = ev.defaultPrevented;   // the editor's own keymap saves when it has focus
      ev.preventDefault();
      if(S.editor && !handled) S.editor.save();
      return;
    }
  });
}

// --- Append to a day ------------------------------------------------------------------------------------
const onHomeView = () => S.route.view === "folder" && S.route.folder === null;
async function appendDay(){
  const ta = $("append-line");
  if(!ta || !S.view || !(S.route.view === "day" || (onHomeView() && S.view.day))) return;
  const gen = S.gen;
  const line = ta.value.replace(/\r?\n+$/, "");
  if(!line.trim()){ toast("type something first"); return; }
  const home = onHomeView();
  const date = home ? S.view.day.date : S.view.date;
  try{
    const d = await api("/api/day/append", {method: "POST", body: JSON.stringify({line, date})});
    if(gen !== S.gen) return;
    ta.value = "";
    if(home) S.view.day = d; else S.view = d;
    render();
    toast("appended to " + (d.today ? "today" : date));
    loadFolders().then(drawRail).catch(() => {});
  }catch(e){
    if(gen === S.gen) toast("append failed: " + e.message);
  }
}

async function createToday(){
  const gen = S.gen;
  try{
    await api("/api/notes/daily/new", {method: "POST", body: "{}"});
    if(gen !== S.gen) return;
    await loadView(false, gen);
    if(gen !== S.gen) return;
    render();
    toast("today's note is here");
    loadFolders().then(drawRail).catch(() => {});
  }catch(e){ if(gen === S.gen) toast(e.message); }
}

// A checkbox ticked in the rendered note: the browser has already flipped it,
// the note follows, and the view reloads to agree with the file.
async function toggleFromRead(cb){
  const r = S.route;
  if(!S.view) return;
  const gen = S.gen;
  const rel = r.view === "note" ? S.view.note.rel : S.view.rel;
  const mtime = r.view === "note" ? S.view.note.mtime : S.view.mtime;
  const done = cb.checked;
  cb.disabled = true;
  try{
    await api("/api/notes/toggle", {method: "POST", body: JSON.stringify({rel, line: +cb.dataset.line, base_mtime: mtime, done})});
    if(gen !== S.gen) return;
    await loadView(false, gen);
    if(gen !== S.gen) return;
    rerender();
    loadFolders().then(drawRail).catch(() => {});
  }catch(e){
    if(gen !== S.gen) return;
    cb.checked = !done; cb.disabled = false;
    toast("could not change that line: " + e.message);
  }
}

// A checkbox ticked in today's page on the notebook home: the same toggle as in the note, then the home re-reads.
async function toggleHomeTask(cb){
  const d = S.view && S.view.home ? S.view.day : null;
  if(!d) return;
  const gen = S.gen;
  const done = cb.checked;
  cb.disabled = true;
  try{
    await api("/api/notes/toggle", {method: "POST", body: JSON.stringify({rel: d.rel, line: +cb.dataset.hometask, base_mtime: d.mtime, done})});
    if(gen !== S.gen) return;
    await loadView(false, gen);
    if(gen !== S.gen) return;
    rerender();
    loadFolders().then(drawRail).catch(() => {});
  }catch(e){
    if(gen !== S.gen) return;
    cb.checked = !done; cb.disabled = false;
    toast("could not change that line: " + e.message);
  }
}

// The search field on the home: the command palette where the shell has one, else the quick switcher.
const hasPalette = () => !!(H && (H.palette || H.cmdk || H.openPalette));
function openPalette(){
  const p = H && (H.palette || H.cmdk);
  if(p && typeof p.open === "function"){ p.open(); return; }
  if(H && typeof H.openPalette === "function"){ H.openPalette(); return; }
  openSwitcher();
}

// The outline follows the reading: the heading nearest the top of the page is the one marked.
function spy(){
  const sc = $("view-notebook"), ol = main() && main().querySelector(".nbx-outline");
  if(!sc) return;
  if(!ol){ sc.onscroll = null; return; }
  const links = [...ol.querySelectorAll("[data-nbsec]")];
  const pick = () => {
    const top = sc.getBoundingClientRect().top + 110;
    let cur = links[0];
    for(const a of links){ const t = $(a.dataset.nbsec); if(t && t.getBoundingClientRect().top <= top) cur = a; }
    links.forEach(a => a.classList.toggle("on", a === cur));
  };
  sc.onscroll = () => { cancelAnimationFrame(S.spy); S.spy = requestAnimationFrame(pick); };
  pick();
}

// --- Editing ---------------------------------------------------------------------------------------------
function toggleEdit(){
  const r = S.route;
  if(readOnly()){ toast("the notebook is read-only at this site (a staging copy or a rollback)"); return; }
  if(r.view === "note" && S.view){
    if(S.editing) H.go(noteHref(r.rel));
    else if(S.view.note.editable) H.go(editHref(r.rel));
    else toast(S.view.note.reason || "this note is read only");
  }else if(r.view === "day" && S.view && S.view.exists){
    if(S.view.today) H.go(editHref(S.view.rel)); else toast("history: a past day's note accepts only appends");
  }
}

const draftKey = rel => "helm.draft." + rel;
function saveDraft(rel, text){ try{ sessionStorage.setItem(draftKey(rel), text); }catch(e){} }
function getDraft(rel){ try{ return sessionStorage.getItem(draftKey(rel)); }catch(e){ return null; } }
function clearDraft(rel){ try{ sessionStorage.removeItem(draftKey(rel)); }catch(e){} }

function showBar(cls, html, buttons, payload){
  const b = $("ebar"); if(!b) return;
  S.ebar = payload || {};
  b.className = "ebar " + cls;
  b.innerHTML = '<span class="ebar-msg">' + html + '</span><span class="ebar-acts">' +
                buttons.map(x => '<button class="btn sm ' + (x[2] ? 'primary' : 'quiet') + '" data-ebar="' + x[0] + '">' + esc(x[1]) + '</button>').join("") + '</span>';
}
function hideBar(){ const b = $("ebar"); if(b){ b.className = "ebar hidden"; b.innerHTML = ""; } S.ebar = null; }
function ebarAction(k){
  const p = S.ebar || {}, ed = S.editor;
  hideBar();
  if(!ed) return;
  if(k === "reload") ed.reload(p.text || "", p.mtime);
  else if(k === "overwrite") ed.overwrite(p.mtime);
  else if(k === "allow") ed.allowSecret().catch(e => toast("still not saved: " + e.message));
  else if(k === "draft"){ clearDraft(S.editRel); ed.view.dispatch({changes: {from: 0, to: ed.view.state.doc.length, insert: p.text || ""}}); ed.focus(); }
  else if(k === "dismiss" && p.draft) clearDraft(S.editRel);
}

async function mountNoteEditor(){
  const n = S.view.note, raw = S.view.raw;
  const mod = await loadEditorModule();
  const host = $("editor-host");
  if(!host || S.editor || !S.view || S.view.note !== n) return;
  const known = new Map((n.outlinks || []).map(l => [String(l.target).toLowerCase(), !!l.rel]));
  S.editRel = n.rel;
  S.editor = mod.mountEditor(host, {
    text: raw.text, mtime: raw.mtime, editable: raw.editable && !readOnly(),
    complete: async (q, kind) => (await api("/api/notes/complete?q=" + encodeURIComponent(q) + "&kind=" + kind)).items,
    attach: f => upload("/api/notes/attach", f),
    openNote: openWikiTarget,
    resolveKnown: t => known.get(String(t).toLowerCase()),
    save: (text, mtime, overwrite, allowSecret) => api("/api/notes/note", {method: "PUT",
      body: JSON.stringify({rel: n.rel, text, base_mtime: overwrite ? null : mtime, allow_secret: !!allowSecret})}),
    onStatus: (t, cls) => { const s = $("estatus"); if(s){ s.textContent = t; s.className = "estatus " + cls; } },
    onConflict: p => showBar("bad", "<b>This note changed on disk</b> since you opened it, so the last edit was not saved.",
                             [["reload", "take the disk version", 1], ["overwrite", "keep mine"]], p),
    onSecret: p => showBar("", "<b>Not saved.</b> A new line looks like a credential (" + esc(p.shape || "") + "), and notes do not carry credentials.",
                           [["allow", "it is not one, save anyway"], ["dismiss", "I will fix the line", 1]], p),
  });
  const draft = getDraft(n.rel);
  if(draft !== null && draft !== raw.text){
    showBar("", "<b>An unsaved draft</b> of this note was left behind when you last navigated away.",
            [["draft", "restore it", 1], ["dismiss", "discard it"]], {text: draft, draft: true});
  }else if(draft !== null) clearDraft(n.rel);
  S.editor.focus();
}

// Ctrl+click on a wikilink in the editor.
async function openWikiTarget(target){
  const t = String(target || "").split("|")[0].split("#")[0].trim();
  if(!t) return;
  try{
    // The server's resolver, the one the read view's links use: a search would cap at a dozen
    // candidates and rank by age, so it could miss the very note the link names.
    const hit = await api("/api/notes/resolve?target=" + encodeURIComponent(t));
    if(!hit.rel){ toast("no note called " + t + " yet"); return; }
    if(hit.kind === "file") window.open(fileHref(hit.rel), "_blank", "noopener"); else H.go(noteHref(hit.rel));
  }catch(e){ toast(e.message); }
}

export function newNoteHere(){
  if(readOnly()){ toast("the notebook is read-only at this site (a staging copy or a rollback)"); return; }
  const r = S.route;
  let folder = r.view === "folder" && r.folder ? r.folder : ((r.view === "note" || r.view === "topic") && r.rel ? folderOf(r.rel) : "");
  if(folder.startsWith("Daily")) folder = "";
  openNewNote(folder);
}

async function openNewNote(folder){
  closeOverlay();
  let tpl = {templates: [], by_folder: {}};
  try{ tpl = await api("/api/notes/templates"); }catch(e){}
  // Where a note may be filed (`allowed`: a new, still-empty folder too), not only the folders
  // listed to read; an older api without it gives the listed folders. Alt+n can land before the
  // boot fetch of the folders has, so read them here rather than offer an empty select.
  if(!S.folders) await loadFolders();
  const folders =(S.folders ? (S.folders.allowed || S.folders.folders.map(f => f.folder)) : []).filter(f => f && !f.startsWith("Daily"));
  if(!folders.includes(folder)) folder = folders[0] || "Reference";
  const o = document.createElement("div"); o.className = "overlay nb-overlay";
  o.innerHTML = '<div class="sw dlg"><h3>New note</h3><input id="nn-title" placeholder="what the note is about" autocomplete="off">' +
    '<div class="row"><select id="nn-folder">' + folders.map(f => '<option' + (f === folder ? ' selected' : '') + '>' + esc(f) + '</option>').join("") + '</select>' +
    '<select id="nn-tpl"></select></div>' +
    '<div class="acts"><span class="hint">Named by its title, filed in the folder, started from the template. Rename later rewrites every link.</span>' +
    '<span class="grow"></span><button class="btn quiet" data-close="1">cancel</button><button class="btn primary" id="nn-go">create</button></div></div>';
  document.body.appendChild(o);
  const fillTpl = () => {
    const d = (tpl.by_folder || {})[$("nn-folder").value];
    $("nn-tpl").innerHTML = '<option value="">' + (d ? 'folder default: ' + esc(d) : 'blank, a heading only') + '</option>' +
      (tpl.templates || []).map(t => '<option value="' + esc(t) + '">' + esc(t) + '</option>').join("");
  };
  fillTpl();
  $("nn-folder").addEventListener("change", fillTpl);
  o.addEventListener("click", ev => { if(ev.target === o || ev.target.dataset.close) closeOverlay(); });
  const submit = async () => {
    const title = $("nn-title").value.trim();
    if(!title){ toast("give it a title"); return; }
    const body = {folder: $("nn-folder").value, title};
    if($("nn-tpl").value) body.template = $("nn-tpl").value;
    try{
      const r = await api("/api/notes/new", {method: "POST", body: JSON.stringify(body)});
      closeOverlay(); toast("created");
      loadFolders().then(drawRail).catch(() => {});
      H.go(editHref(r.rel));
    }catch(e){ toast(e.message); }
  };
  $("nn-go").addEventListener("click", submit);
  $("nn-title").addEventListener("keydown", ev => { if(ev.key === "Enter"){ ev.preventDefault(); submit(); } });
  $("nn-title").focus();
}

// Rename is two steps: a dry run that lists what would be rewritten, then the move.
async function openRename(rel){
  if(S.editor && S.editRel === rel){ await S.editor.flush(); }
  closeOverlay();
  const stem = rel.replace(/\.md$/i, "").split("/").pop();
  const o = document.createElement("div"); o.className = "overlay nb-overlay";
  o.innerHTML = '<div class="sw dlg"><h3>Rename</h3><input id="rn-name" value="' + esc(stem) + '" autocomplete="off">' +
    '<div class="plan" id="rn-plan">Enter shows what would change before anything moves.</div>' +
    '<div class="acts"><span class="grow"></span><button class="btn quiet" data-close="1">cancel</button>' +
    '<button class="btn" id="rn-check">check</button><button class="btn primary hidden" id="rn-go">rename</button></div></div>';
  document.body.appendChild(o);
  o.addEventListener("click", ev => { if(ev.target === o || ev.target.dataset.close) closeOverlay(); });
  let plan = null;
  const check = async () => {
    try{
      plan = await api("/api/notes/rename", {method: "POST", body: JSON.stringify({rel, new_stem: $("rn-name").value.trim(), dry_run: true})});
      const daily = plan.changes.filter(c => c.daily).length;
      const n = plan.changes.length;
      $("rn-plan").innerHTML = '<b>' + esc(plan.from) + '</b> becomes <b>' + esc(plan.to) + '</b>.' +
        (plan.total ? ' ' + plan.total + ' link' + (plan.total === 1 ? '' : 's') + ' in ' + n + ' note' + (n === 1 ? '' : 's') + ' will be rewritten' +
          (daily ? ', including ' + daily + ' daily note' + (daily === 1 ? '' : 's') + ' (history, but a link that breaks is worse)' : '') + ':<ul>' +
          plan.changes.map(c => '<li>' + esc(c.rel) + ' <span class="muted">' + c.count + '</span></li>').join("") + '</ul>'
        : ' Nothing links to it, so only the file moves.');
      $("rn-go").classList.remove("hidden");
    }catch(e){ plan = null; $("rn-plan").textContent = e.message; $("rn-go").classList.add("hidden"); }
  };
  const doIt = async () => {
    if(!plan) return;
    try{
      if(S.editor && S.editRel === rel){ await S.editor.flush(); dropEditor(); }
      const r = await api("/api/notes/rename", {method: "POST", body: JSON.stringify({rel, new_stem: plan.new_stem, dry_run: false})});
      const missed = r.links_not_rewritten || [];
      closeOverlay(); toast(missed.length
        ? "renamed, but " + missed.length + " linking page" + (missed.length === 1 ? "" : "s") + " kept changing and still link the old name: " + missed.join(", ")
        : "renamed" + (r.total ? ", " + r.total + " link" + (r.total === 1 ? "" : "s") + " rewritten" : ""));
      loadFolders().then(drawRail).catch(() => {});
      H.go(noteHref(r.to));
    }catch(e){ toast(e.message); }
  };
  $("rn-check").addEventListener("click", check);
  $("rn-go").addEventListener("click", doIt);
  $("rn-name").addEventListener("keydown", ev => { if(ev.key === "Enter"){ ev.preventDefault(); plan ? doIt() : check(); } });
  $("rn-name").addEventListener("input", () => { plan = null; $("rn-go").classList.add("hidden"); });
  $("rn-name").focus(); $("rn-name").select();
}

// --- Switcher (Ctrl+O) -------------------------------------------------------------------------
function closeOverlay(){ document.querySelectorAll(".overlay.nb-overlay").forEach(o => o.remove()); }
export function openSwitcher(){
  closeOverlay();
  const o = document.createElement("div");
  o.className = "overlay nb-overlay";
  o.innerHTML = '<div class="sw"><input id="sw-q" placeholder="jump to a note or a thread" autocomplete="off"><div class="res" id="sw-res"></div></div>';
  document.body.appendChild(o);
  o.addEventListener("click", ev => { if(ev.target === o) closeOverlay(); });
  const inp = $("sw-q"), res = $("sw-res");
  let rows = [], sel = 0, seq = 0;
  const draw = () => {
    res.innerHTML = rows.map((r, i) => '<div class="r' + (i === sel ? ' sel' : '') + '" data-i="' + i + '"><span class="kind">' + esc(r.kind) +
      '</span><span>' + esc(r.label) + '</span><span class="where">' + esc(r.where) + '</span></div>').join("") ||
      '<div class="r muted">' + (inp.value ? 'nothing matches' : 'type to search notes and threads') + '</div>';
  };
  const pick = i => { const r = rows[i]; if(!r) return; closeOverlay(); H.go(r.href); };
  const run = async () => {
    const q = inp.value.trim(), my = ++seq;
    if(!q){ rows = []; draw(); return; }
    try{
      const v = await api("/api/switcher?q=" + encodeURIComponent(q));
      if(my !== seq) return;
      rows = (v.notes || []).map(n => ({kind: "note", label: n.title || n.stem, where: n.folder, href: noteHref(n.rel)}))
        .concat((v.threads || []).map(t => ({kind: t.state === "needs-you" ? "lit" : "thread", label: t.title, where: t.category || "", href: threadHref(t.thread_id)})));
      sel = 0; draw();
    }catch(e){ rows = []; draw(); }
  };
  let h; inp.addEventListener("input", () => { clearTimeout(h); h = setTimeout(run, 120); });
  inp.addEventListener("keydown", ev => {
    if(ev.key === "ArrowDown"){ ev.preventDefault(); sel = Math.min(rows.length - 1, sel + 1); draw(); }
    else if(ev.key === "ArrowUp"){ ev.preventDefault(); sel = Math.max(0, sel - 1); draw(); }
    else if(ev.key === "Enter"){ ev.preventDefault(); pick(sel); }
    else if(ev.key === "Escape"){ closeOverlay(); }
  });
  res.addEventListener("click", ev => { const r = ev.target.closest("[data-i]"); if(r) pick(+r.dataset.i); });
  draw(); inp.focus();
}

// --- Start -----------------------------------------------------------------------------------------------
export function init(shell){
  H = shell;
  wire();
}
export {S as state};
