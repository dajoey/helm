// Shared helpers for the notebook module: the API call, escaping,
// ages, toasts, preferences. Same-origin through the web role's proxy: no bearer in
// the page any more (the token lives in app/web.py and never reaches the browser).

// A lapsed sign-in is renewed and the call retried by app.js's ssoFetch (the hourly Authentik
// lapse used to fail a save as "Failed to fetch"); plain fetch where the shell is absent.
// The app's own name, from the head the web role serves.
export const appName = () => (document.querySelector('meta[name="application-name"]') || {}).content || "Helm";

const sso = (path, opts) => (typeof window.ssoFetch === "function" ? window.ssoFetch(path, opts) : fetch(path, opts));

export async function api(path, opts){
  const r = await sso(path, Object.assign({headers: {"Content-Type": "application/json"}, credentials: "same-origin"}, opts || {}));
  let body = null;
  try { body = await r.json(); } catch(e) { body = {detail: "no JSON body"}; }
  if(!r.ok){
    const err = new Error(r.status === 405 ? (body.detail || "the notebook is read-only at this site") : (body.detail || body.error || ("HTTP " + r.status)));
    err.status = r.status; err.payload = body;
    throw err;
  }
  return body;
}

// Raw upload: the body is the file, the name rides in a header.
export async function upload(path, file){
  const r = await sso(path, {method: "POST", credentials: "same-origin", headers: {
    "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name || "")}, body: file});
  let body = null;
  try { body = await r.json(); } catch(e) { body = {detail: "no JSON body"}; }
  if(!r.ok){
    const err = new Error(body.detail || body.error || ("HTTP " + r.status));
    err.status = r.status; err.payload = body;
    throw err;
  }
  return body;
}

export const esc = s => String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");

// Bare http(s) URLs in prose become links; everything else is escaped text.
const URL_RE = /https?:\/\/[^\s<>"')]+/g;
export function richText(s){
  s = String(s == null ? "" : s);
  let out = "", last = 0, m;
  URL_RE.lastIndex = 0;
  while((m = URL_RE.exec(s))){
    let url = m[0], trail = "";
    while(url.length && ".,;:!?)]}".includes(url[url.length - 1])){
      trail = url[url.length - 1] + trail;
      url = url.slice(0, -1);
    }
    out += esc(s.slice(last, m.index));
    out += '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + esc(url) + '</a>';
    out += esc(trail);
    last = m.index + m[0].length;
  }
  out += esc(s.slice(last));
  return out;
}

// FTS snippets arrive with \x02 and \x03 around the matched words.
export function markSnippet(s){
  return esc(s).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>");
}

export function age(ts){
  if(!ts) return "";
  const s = Math.max(0, Date.now()/1000 - ts);
  if(s < 90) return "just now";
  if(s < 5400) return Math.round(s/60) + "m ago";
  if(s < 172800) return Math.round(s/3600) + "h ago";
  return Math.round(s/86400) + "d ago";
}
// How long a lit card has sat. Three days is a nudge, a week is a problem.
export function ageClass(t){
  if(t.state !== "needs-you") return "";
  const d = (Date.now()/1000 - (t.last_change || 0)) / 86400;
  return d >= 7 ? " stale" : d >= 3 ? " old" : "";
}
// 12-hour, "9:12 AM", at most eight characters; the time column is sized to fit it on one line.
export function clock(ts){
  if(!ts) return "";
  const d = new Date(ts*1000);
  const h = d.getHours();
  return String(h % 12 || 12) + ":" + String(d.getMinutes()).padStart(2, "0") + (h < 12 ? " AM" : " PM");
}
// An option id as words: snooze_again -> snooze again. Helm drops an option's
// wording the moment a thread is answered, so past answers often have only the id.
export function optWords(v){
  if(v === "__redirect__") return "with a note";
  if(v === "__task__") return "handed it over";
  if(v === "ack") return "acknowledged";
  if(v === "note-only") return "a note only";
  return String(v == null ? "" : v).split(/[_-]+/).filter(Boolean).join(" ");
}
export function localDate(d){
  const x = d instanceof Date ? d : new Date(d);
  return x.getFullYear() + "-" + String(x.getMonth()+1).padStart(2,"0") + "-" +
         String(x.getDate()).padStart(2,"0");
}
export function shortDate(iso){
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString([], {weekday:"short", month:"short", day:"numeric"});
}

export function toast(msg, ms){
  const t = document.getElementById("toast");
  if(!t) return;
  t.textContent = msg; t.classList.add("on");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), ms || 2800);
}

// Preferences. Every read is wrapped: blocked site data loses the preference
// and nothing else.
export function lsGet(k, d){ try{ const v = localStorage.getItem(k); return v === null ? d : v; }catch(e){ return d; } }
export function lsSet(k, v){ try{ localStorage.setItem(k, v); }catch(e){} }
export function lsSetJSON(k, v){ lsSet(k, JSON.stringify(v)); }
export function lsGetSet(k){
  try{ const a = JSON.parse(lsGet(k, "[]")); return new Set(Array.isArray(a) ? a : []); }
  catch(e){ return new Set(); }
}

export const open = lsGetSet("helm.nb.open");
export const closedGroups = lsGetSet("helm.nb.closedGroups");
export const histOpen = new Set();   // per page load on purpose
export function saveOpen(){ lsSetJSON("helm.nb.open", [...open]); lsSetJSON("helm.nb.closedGroups", [...closedGroups]); }

// Routes are the shell's (shell.js): a thread opens as its conversation overlay.
export const noteHref = rel => "#/note/" + encodeURIComponent(rel);
export const threadHref = tid => "#/conv/" + encodeURIComponent(tid);
export const dayHref = date => "#/day/" + date;
// The bare notebook hash is the composed home; an explicit empty folder query
// is the root folder. Preserve that empty string when the shell hands it back.
export const folderHref = f => f ? "#/notebook/" + encodeURIComponent(f) : "#/notebook?folder=";
export const folderFromRoute = f => f == null ? null : f;
export const editHref = rel => "#/edit/" + encodeURIComponent(rel);
export const topicHref = rel => "#/topic/" + encodeURIComponent(rel);
// Match notebook/render.py: the whole relative path is encoded, including slashes.
export const fileHref = rel => "/att/" + encodeURIComponent(String(rel || ""));

// The vault folder and the folders it used to live in (vault_aliases: an old card
// cites a file by a host path), learned from /api/notes/meta, so a link that
// carries the absolute path of a note can be opened in-app. app.js noteRelFromUrl is
// the same rule for the conversation's properties.
let vaultPrefixes = [];
export function setVault(path, aliases){
  vaultPrefixes = [path].concat(aliases || [])
    .map(p => String(p || "").replace(/\\/g, "/").toLowerCase().replace(/\/+$/, ""))
    .filter(Boolean).map(p => p + "/");
}
export function noteRelFromUrl(url){
  const u = String(url || "").trim();
  if(/^note:/i.test(u)){
    let rel = u.slice(5).trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if(!/\.md$/i.test(rel)) rel += ".md";
    return rel;
  }
  const n = u.replace(/\\/g, "/");
  if(!/\.md$/i.test(n)) return null;
  const p = vaultPrefixes.find(v => n.toLowerCase().startsWith(v));
  return p ? n.slice(p.length) : null;
}

export function typing(){
  const a = document.activeElement;
  return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable);
}

// ---- Chart Room helpers: object chips, dates, plain-word sizes --------------------------------
// Every link to another object is a chip (Helm.chip, web/objects.js). When the chip builder is not
// there (a page the shell has not finished), a plain anchor takes its place so nothing is lost.
export function chipHtml(spec){
  const Hh = window.Helm;
  if(Hh && typeof Hh.chipHTML === "function") return Hh.chipHTML(spec);
  const label = spec.label != null && spec.label !== "" ? spec.label : (spec.id != null ? spec.id : "");
  return '<a class="hm-ochip t-' + esc(spec.type || "note") + '"' + (spec.href ? ' href="' + esc(spec.href) + '"' : "") + '>' + esc(label) + '</a>';
}
export function glyphHtml(type, size){
  const Hh = window.Helm;
  return Hh && typeof Hh.glyphHTML === "function" ? Hh.glyphHTML(type, size) : "";
}
export const stemOf = rel => String(rel || "").replace(/\.md$/i, "").split("/").pop();
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const noteChip = (rel, label) => DATE_RE.test(stemOf(rel))
  ? chipHtml({type: "day", id: stemOf(rel), label: label || stemOf(rel), href: dayHref(stemOf(rel))})
  : chipHtml({type: "note", id: rel, label: label || String(rel).replace(/\.md$/i, ""), href: noteHref(rel)});
export const dayChip = date => chipHtml({type: "day", id: date, label: date, href: dayHref(date)});
export const threadChip = t => chipHtml({
  type: t.state === "needs-you" ? "ask" : "thread", id: t.thread_id, label: t.title || t.thread_id,
  href: threadHref(t.thread_id), state: t.state === "needs-you" ? "Waiting on you" : ""});
export const taskChip = (id, label, extra) => chipHtml(Object.assign({type: "task", id, label: label || id, href: "#/board/" + encodeURIComponent(id)}, extra || {}));

export function plural(n, one, many){ return n + " " + (n === 1 ? one : (many || one + "s")); }
export function kb(size){
  const n = Number(size) || 0;
  return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? Math.round(n / 1024) + " KB" : n + " B";
}
export function readMins(size){ return Math.max(1, Math.round((Number(size) || 0) / 6 / 230)); }
export function startOfToday(){ const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; }
export function longDate(iso){
  const d = new Date(iso + "T12:00:00");
  // the month and the day number stay together when the title wraps
  return d.toLocaleDateString([], {weekday: "long", day: "numeric", month: "long"}).replace(/ (\S+)$/, "\u00a0$1");
}
export function monthName(ym){
  const d = new Date(ym + "-15T12:00:00");
  return isNaN(d) ? ym : d.toLocaleDateString([], {month: "long", year: "numeric"});
}
// "Today 9:48 PM", "Yesterday 7:15 PM", "16 Nov 12:26 AM": the time column of a log.
export function whenParts(ts){
  if(!ts) return {day: "", time: ""};
  const d = new Date(ts * 1000), t0 = startOfToday() * 1000;
  const day = d.getTime() >= t0 ? "Today" : d.getTime() >= t0 - 86400000 ? "Yesterday"
    : d.toLocaleDateString([], {day: "numeric", month: "short"});
  return {day, time: clock(ts)};
}
// A log line as plain words: list and checkbox marks, wikilink brackets, bold marks and link syntax dropped.
export function stripMd(s){
  return String(s == null ? "" : s)
    .replace(/^\s*(?:[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|#{1,6}\s+|>\s+)/, "")
    .replace(/\[\[([^\]|#]*)(?:#[^\]|]*)?\|([^\]]+)\]\]/g, "$2")
    .replace(/\[\[([^\]|#]+)(?:#[^\]]*)?\]\]/g, (m, t) => t.split("/").pop())
    .replace(/\[([^\]]+)\]\((?:[^)]+)\)/g, "$1")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}
// Who wrote a line: the pinned principal, else the source; the owner's own names read as "You".
export function whoName(principal, source){
  const p = String(principal || source || "").trim();
  if(!p) return "";
  return /^(owner|you|owner|owner|human)$/i.test(p) ? "You" : p;
}
// A date at the head of a log line ("2026-11-17 10:02 text", "[2026-11-11] text") becomes the line's time.
export function lineStamp(text){
  const m = /^\s*[\[(]?(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?[\])]?\s*[:\-–—]?\s*(.*)$/.exec(text || "");
  return m ? {date: m[1], time: m[2] || "", rest: m[3]} : null;
}
