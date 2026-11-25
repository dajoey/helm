/* Helm Triage web UI — the inbox's data layer (helm-web v43, reshaped for the three-pane app,
   ). What stays from v43 unchanged: the 60 s poll with the cached copy and the red
   banner, the classification (NEEDS YOU / IN FLIGHT / ON HOLD / BACKLOG / done), drafts that
   survive a refresh, the v10 submit gating, the file-a-card sheet, the lightbox, the body
   renderer and the toasts. What moved: the cards are gone — a thread is drawn once, as a row
   in a left-pane list (needsyou.js, threads.js) and as its conversation in the middle pane
   (conversation.js), where every affordance a card had now lives. Talks only to the
   same-origin proxy (app/web.py). At work a tap is stamped `by: "owner"` (the work rule
   pack's attribution).                                                                    */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

const NOTE_ONLY = "note-only";          // same sentinel the app sends for "Send note only"
const URG = { now: 0, "this-week": 1, whenever: 2 };
const URGENCIES = [["whenever", "whenever"], ["this-week", "this week"], ["now", "now"]];
// The intents for a new card. The sheet files with intent
// "agent" (v21: a card the owner types is work for an agent), so the row is not drawn there.
const INTENTS = [["agent", "hand it to an agent"], ["mine", "mine to deal with"], ["waiting", "waiting on someone"]];
const INTENT_HINT = {
  agent: "Lands under IN FLIGHT for an agent — it does not light up your own queue.",
  mine: "Goes on the list waiting on you, with one button: Done.",
  waiting: "Filed as waiting on someone else, so it asks you nothing and never lights up.",
};
// Sheet pickers are chips, not <select> (v13) — see index.html. Selection is
// plain state, persisted with the title/details drafts so a reload keeps it.
const sheetPick = { urgency: "whenever", intent: "agent" };
const isWork = () => (typeof Helm !== "undefined" && Helm.site === "work");
// The installed app's name, from the head the web role serves (app/web.py APP_HEAD).
const APP_NAME = (document.querySelector('meta[name="application-name"]') || {}).content || "Helm";

// Storage the browser blocks for this origin throws on a READ too (a SecurityError from the getter),
// not only on a write. These are per-viewer conveniences: blocked reads as unset, never a dead page.
const stored = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const storedJSON = (k, dflt) => { try { return JSON.parse(stored(k) || "null") ?? dflt; } catch { return dflt; } };

const state = {
  threads: [], generatedAt: 0, loading: false, offline: false, offlineWhy: "",
  emergenciesOnly: stored("helm.emerg") === "1",
  // Every thread ever held lives under THREADS (the index route); the live read stays light.
  showDone: false,
  gateIds: new Set(), gateCount: 0,
  categories: new Set(storedJSON("helm.cats", [])),
};

// ---------- folds ----------
// A fold is remembered per key (a thread's run block, a properties group, a list section), so
// the 60 s rebuild keeps them and "collapse all" does not undo itself (2026-09-21).
let folds = {};
try { folds = JSON.parse(localStorage.getItem("helm.fold") || "{}"); } catch { folds = {}; }
const saveFolds = () => { try { localStorage.setItem("helm.fold", JSON.stringify(folds)); } catch { /* per-viewer convenience only */ } };
const isFolded = (key, dflt) => (key in folds) ? !!folds[key] : !!dflt;
const setFold = (key, folded) => { folds[key] = folded ? 1 : 0; saveFolds(); };

// ---------- drafts ----------
// Anything being typed died on the next refresh. Drafts live in localStorage keyed by thread
// ("<id>:note") or sheet field ("c-title"), are restored on every render (focus + caret
// included), and are cleared only when the answer/note is actually accepted.
const DRAFTS_KEY = "helm.drafts";
let drafts = {};
try { drafts = JSON.parse(localStorage.getItem(DRAFTS_KEY) || "{}"); } catch { drafts = {}; }
// Storage full or blocked: the draft lives in memory only. A throw here used to stop a send before it began.
const saveDrafts = () => { try { localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts)); } catch { /* per-viewer convenience */ } };
document.addEventListener("input", (e) => {
  const t = e.target;
  if (!(t instanceof HTMLTextAreaElement) && !(t instanceof HTMLInputElement)) return;
  if (t.dataset.draft) { drafts[t.dataset.draft] = t.value; saveDrafts(); return; }
  if (/^c-(title|details|category|link)$/.test(t.id)) { drafts[t.id] = t.value; saveDrafts(); }
});

// ---------- API ----------
// helm.example.com sits behind Authentik forward_auth (2026-09-03), and the proxy session lasts
// one hour (the provider's access-token validity). When it lapses every call is answered with a
// 302 toward Authentik's sign-in (auth.example.com, or /outpost.goauthentik.io/start), which a
// fetch cannot follow: it lands cross-origin. Until 2026-09-26 the answer was to send the whole tab through the sign-in; with
// Authentik itself still signed in that bounced straight back, so once an hour the page "refreshed
// by itself", landed on NEEDS YOU and took whatever was being typed with it (the owner lost a front-desk
// message that way). Now a hidden frame renews the session (signed in, the chain is redirects only
// and ends back on this origin) and the call is retried; a call refused by forward_auth never
// reached the app, so the retry cannot double anything. Only when Authentik is signed out as well
// (its sign-in page refuses to be framed) must the tab go, and then it waits for a click while
// anything is typed, and comes back to the same screen either way.
const RENEW_LANDING = "/api/meta";   // where the hidden frame's chain ends: small and same-origin
let renewing = null, renewFailedAt = 0, signedOutAt = -1;
function isAuthRedirect(r) {
  const loc = r.headers.get("Location") || "";
  return r.type === "opaqueredirect" || r.status === 0 ||
    (r.status === 302 && (loc.startsWith("https://auth.example.com") || loc.startsWith("/outpost.goauthentik.io")));
}
function renewSession() {
  if (renewing) return renewing;   // every call that met the lapse waits on the same frame
  if (Date.now() - renewFailedAt < 60_000) return Promise.resolve(false);   // signed out: not a frame per poll
  renewing = new Promise((resolve) => {
    const f = document.createElement("iframe");
    f.hidden = true; f.tabIndex = -1; f.setAttribute("aria-hidden", "true");
    let settled = false, timer = null;
    const done = (ok) => {
      if (settled) return;
      settled = true; clearTimeout(timer); f.remove();
      if (!ok) renewFailedAt = Date.now();
      resolve(ok);
    };
    timer = setTimeout(() => done(false), 15_000);
    f.addEventListener("load", () => {
      let path = null;
      try {
        if (f.contentWindow.location.href === "about:blank") return;   // the frame's first, empty document
        path = f.contentWindow.location.pathname;
      } catch { /* still on Authentik (its sign-in page, or the framing refusal): cross-origin */ }
      done(path === RENEW_LANDING);
    });
    f.src = "/outpost.goauthentik.io/start?rd=" + encodeURIComponent(RENEW_LANDING);
    document.body.append(f);
  }).finally(() => { renewing = null; });
  return renewing;
}
function unsentText() {
  const a = document.activeElement;
  if (a && (a.isContentEditable || a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && /^(text|search|url|email)$/.test(a.type)))) return true;
  return [...document.querySelectorAll("textarea")].some((t) => t.value.trim() && t.offsetParent !== null);
}
function signIn() {
  location.assign("/outpost.goauthentik.io/start?rd=" + encodeURIComponent(location.pathname + location.search + location.hash));
}
function signedOut() {
  if (!unsentText()) {
    let n = 2;
    try { n = +(sessionStorage.getItem("ssoTries") || 0); sessionStorage.setItem("ssoTries", n + 1); } catch { /* no storage: ask instead */ }
    if (n < 2) { signIn(); return; }   // stop loops
  }
  let b = document.querySelector(".sso-banner");
  if (!b) {
    b = document.createElement("div");
    b.className = "sso-banner"; b.setAttribute("role", "alert");
    b.innerHTML = '<span>Your sign-in ended. Nothing you typed is lost.</span><button class="sso-signin btn primary" type="button">Sign in again</button>';
    b.querySelector(".sso-signin").onclick = signIn;
    document.body.append(b);
  }
  b.hidden = false;
  signedOutAt = performance.now();
}
// Every same-origin call goes through here (the notebook and settings too): a lapsed session is
// renewed and the call retried once; the Response is the caller's as fetch() would return it.
async function ssoFetch(path, opts = {}) {
  let started = performance.now();
  const go = () => { started = performance.now(); return fetch(path, { credentials: "same-origin", ...opts, redirect: "manual" }); };
  let r = await go();
  if (isAuthRedirect(r)) {
    if (await renewSession()) r = await go();
    if (isAuthRedirect(r)) { signedOut(); throw new Error("signed out - sign in again to continue"); }
  }
  // Signed in again (another tab, or the renewal) only if a call sent AFTER the notice went through:
  // one already in flight when the session lapsed proves nothing.
  if (started > signedOutAt) {
    try { sessionStorage.removeItem("ssoTries"); } catch { /* no storage */ }
    const b = document.querySelector(".sso-banner");
    if (b) b.hidden = true;
  }
  return r;
}
async function api(path, opts = {}) {
  const r = await ssoFetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null; try { body = await r.json(); } catch { /* non-json */ }
  if (!r.ok) {
    const d = body && (body.detail || body.error);
    throw new Error(`${r.status} ${typeof d === "string" ? d : JSON.stringify(d || body || "")}`);
  }
  return body;
}

// Hygiene (): on a fresh read, drop the drafts ("<tid>:note") and folds
// ("<tid>|...") of threads no list holds any more: not in the live read, not on a THREADS page
// already read, not open. A done thread still open in THREADS keeps its draft. While THREADS is
// on screen and its first read is out, nothing is dropped (the next poll does it).
function pruneOrphans() {
  const listed = typeof window.threadsListed === "function" ? window.threadsListed() : new Set();
  if (!listed) return;
  const held = new Set([...listed, ...state.threads.map(t => t.thread_id)]);
  const open = typeof window.conversationOpen === "function" && window.conversationOpen();
  if (open) held.add(open);
  const routed = typeof Helm !== "undefined" && Helm.parseHash ? Helm.parseHash().tid : null;
  if (routed) held.add(routed);
  let pruned = false;
  for (const k of Object.keys(drafts)) if (k.endsWith(":note") && !held.has(k.slice(0, -5))) { delete drafts[k]; pruned = true; }
  if (pruned) saveDrafts();
  pruned = false;
  for (const k of Object.keys(folds)) if (k.includes("|") && !held.has(k.split("|")[0])) { delete folds[k]; pruned = true; }
  if (pruned) saveFolds();
}

async function load() {
  if (state.loading) return;
  state.loading = true; $("#refresh").classList.add("spin");
  try {
    if (typeof Helm !== "undefined" && Helm.ready) await Helm.ready;   // the site decides the query
    // The live set is ~30KB; the done history is ~750KB. Poll the light one; THREADS pages
    // the history through the index route on demand.
    // include_snoozed: a snoozed waiting-external thread is still in flight and the board's
    // PARKED tile is the one place it can be seen. It stays out of NEEDS YOU below.
    // include_backlog: the backlog is the "kept with its question" pile.
    const qs = "?include_snoozed=true" + (state.showDone ? "&include_done=true" : "") + (isWork() ? "&include_backlog=true" : "");
    const d = await api("/api/triage/threads" + qs);
    let threads = d.threads || [];
    // Needs-the owner gate (2026-09-25, home only): what it kept from him in the last 14 days.
    if (!isWork()) {
      try {
        const g = await api("/api/triage/gate?days=14");
        const kept = (g.entries || []).filter(e => e.decision === "held" || e.decision === "settled");
        state.gateIds = new Set(kept.map(e => e.thread_id)); state.gateCount = kept.length;
      } catch { state.gateIds = new Set(); state.gateCount = 0; }
    } else { state.gateIds = new Set(); state.gateCount = 0; }
    if (!state.showDone) threads = threads.filter(t => t.state !== "done");
    state.threads = threads;
    state.generatedAt = d.generated_at || Date.now() / 1000;
    state.offline = false; state.offlineWhy = "";
    pruneOrphans();
    // The offline copy is a convenience: storage full or blocked is never an outage (it used to paint
    // CAN'T REACH THE GATEWAY and refuse every answer; helm-web learned the same on 2026-09-26).
    try { localStorage.setItem("helm.cache", JSON.stringify({ t: state.generatedAt, threads: state.threads })); } catch { /* no offline copy */ }
    if (d.woke) toast(`${d.woke} snoozed thread${d.woke > 1 ? "s" : ""} woke up`);
  } catch (e) {
    state.offline = true; state.offlineWhy = e.message;
    if (!state.threads.length) {
      const j = storedJSON("helm.cache", null);
      if (j && Array.isArray(j.threads)) { state.threads = j.threads; state.generatedAt = j.t; }
    }
  } finally {
    state.loading = false; $("#refresh").classList.remove("spin"); render();
  }
}

// Pull-to-refresh in the app = POST /resort then re-read. {} resorts every category (202).
async function resort() {
  try { await api("/api/triage/resort", { method: "POST", body: "{}" }); } catch { /* not fatal */ }
  await load();
}

// ---------- classification (mirrors TriageState.kt) ----------
function isSnoozed(t) { return t.wake_at && t.wake_at * 1000 > Date.now(); }
function section(t) {
  if (t.state === "done") return "recently-done";
  if (t.state === "needs-you") return "needs-you";
  if (t.state === "working") return "in-flight";
  if (t.state === "backlog") return "backlog";   // work: kept with its question, back when pulled
  return "parked"; // waiting-external, incl. snoozed
}
function hasAsk(t) { return t.ask && t.ask.kind && t.ask.kind !== "none"; }
function gateOf(t) {
  // The gate's own latest word on this thread ("held by gate ..." / "settled by gate ..."), if any.
  const ev = (t.events || []).filter(e => e.source === "helm-gate" && e.kind === "state-change");
  return ev.length ? ev[ev.length - 1].body : "";
}
function filteredActive() {
  let base = state.threads.filter(t => t.state !== "done");
  if (state.emergenciesOnly) base = base.filter(t => t.state === "needs-you" && t.urgency === "now");
  if (state.categories.size) base = base.filter(t => state.categories.has((t.category || "").trim()));
  return base;
}
function needsYou() { return filteredActive().filter(t => t.state === "needs-you" && !isSnoozed(t)).sort((a, b) => (URG[a.urgency] ?? 3) - (URG[b.urgency] ?? 3) || b.last_change - a.last_change); }
function inFlight() { return filteredActive().filter(t => t.state === "working" && !isSnoozed(t)).sort((a, b) => b.last_change - a.last_change); }
function parked() { return filteredActive().filter(t => t.state === "waiting-external").sort((a, b) => ((a.recheck_at || Infinity) - (b.recheck_at || Infinity)) || b.last_change - a.last_change); }
function backlog() { return filteredActive().filter(t => t.state === "backlog").sort((a, b) => (a.last_change || 0) - (b.last_change || 0)); }
const emergencyCount = () => state.threads.filter(t => t.state === "needs-you" && t.urgency === "now" && !isSnoozed(t)).length;
const youCount = () => state.threads.filter(t => t.state === "needs-you" && !isSnoozed(t)).length;
const threadById = (tid) => state.threads.find(t => t.thread_id === tid) || null;

// The header's NEED YOU pill, the window title and the app badge: Helm's own needs-you cards plus whatever else is waiting
// on him (Helm.extraNeeds(), for extras that add asks). The rail's count adds the same number.
function paintYou() {
  const extra = typeof Helm !== "undefined" && typeof Helm.extraNeeds === "function" ? (Number(Helm.extraNeeds()) || 0) : 0;
  const you = youCount() + extra;
  $("#you-n").textContent = you; $("#you").classList.toggle("lit", you > 0);
  document.title = you ? `(${you}) ${APP_NAME}` : APP_NAME;
  if (navigator.setAppBadge) { you ? navigator.setAppBadge(you) : navigator.clearAppBadge?.(); }
}
if (typeof Helm !== "undefined") Helm.paintYou = paintYou;

// ---------- render: the HUD, the banner, and a word to every list ----------
function render() {
  document.body.classList.toggle("offline", state.offline);
  const fire = emergencyCount(), you = youCount();
  $("#fire-n").textContent = fire; $("#fire").classList.toggle("hot", fire > 0);
  paintYou();
  if (typeof Helm !== "undefined" && Helm.setNeeds) Helm.setNeeds(you);

  const b = $("#banner");
  if (state.offline) {
    b.className = "banner"; b.textContent = `CAN'T REACH THE GATEWAY — answers won't send. ${state.offlineWhy}${state.threads.length ? " · showing copy from " + fmtAge(state.generatedAt) : ""} · tap to retry`;
    b.onclick = () => load();
  } else b.className = "banner hidden";

  // Prune any stored category filter that no longer exists among open issues.
  const cats = new Set(state.threads.filter(t => t.state !== "done").map(t => (t.category || "").trim()).filter(Boolean));
  for (const c of [...state.categories]) if (!cats.has(c)) state.categories.delete(c);
  if (typeof Helm !== "undefined") Helm.emit("threads", state);
}

function emptyView(kind, big, sub) {
  const e = el("div", "empty " + kind); e.append(el("span", "big", big)); if (sub) e.append(el("span", "sub", sub)); return e;
}

// ---------- submit gating (fix 2026-09-04 "answers submitted while I was hovering") ----------
let pressBtn = null;
document.addEventListener("pointerdown", (e) => {
  pressBtn = e.target.closest ? e.target.closest("button[data-submit]") : null;
  if (pressBtn && pressBtn.disabled) pressBtn = null;
}, true);
document.addEventListener("pointerup", (e) => {
  const b = pressBtn; pressBtn = null;
  if (!b || b.disabled || !b.contains(e.target)) return;
  b._act && b._act();
});
document.addEventListener("pointercancel", () => { pressBtn = null; }, true);
document.addEventListener("keydown", (e) => {
  if (e.repeat) return;
  const b = e.target.closest ? e.target.closest("button[data-submit]") : null;
  if (!b || b.disabled) return;
  if (e.key === " ") { e.preventDefault(); return; }
  if (e.key === "Enter") { e.preventDefault(); b._act && b._act(); }
});
function markSubmit(b, fn) {
  b.type = "button";
  b.setAttribute("data-submit", "");
  b._act = fn ? () => {
    if (b.dataset.submitDone === "1") return;
    b.dataset.submitDone = "1";
    setTimeout(() => { delete b.dataset.submitDone; }, 700);
    fn();
  } : null;
}
function optButton(label, effect, fn, extraClass = "") {
  const b = el("button", "opt" + (extraClass ? " " + extraClass : ""));
  b.append(el("span", "lbl", label)); if (effect) b.append(el("span", "eff", effect));
  markSubmit(b, fn); return b;
}
function classifyOption(o) {
  const s = ((o.label || "") + " " + (o.value || "") + " " + (o.type || "")).toLowerCase();
  if (/\b(approve|yes|accept|proceed|grant|ship|merge|lift|go-ahead)\b/.test(s)) return "opt-approve";
  if (/\b(deny|no|reject|cancel|stop|halt|close|drop)\b/.test(s)) return "opt-deny";
  if (/\b(amend|modify|change|edit|different|custom)\b/.test(s)) return "opt-amend";
  return "";
}

const SNOOZE_OPTIONS = [
  ["1h", 1, "Snoozed 1h — back in an hour"],
  ["1d", 24, "Snoozed 1d — back tomorrow"],
  ["1w", 168, "Snoozed 1w — back next week"],
  ["1mo", 720, "Snoozed 1mo — back in a month"],
];

// A tap on a thread: reply / close / mute / snooze / reopen / park / unpark / backlog /
// unbacklog through the store's own routes. One global lock, the draft cleared only on
// acceptance, the board re-read after.
let acting = false;
async function act(node, t, verb, body, okMsg, transform) {
  if (acting) return toast("already sending — hold on", true);
  if (state.offline) return toast("gateway unreachable — not sent", true);
  acting = true; if (node) node.classList.add("busy");
  try {
    if (transform) body = await transform(body);
    // Work rule pack: `by` says who answered. A tap on this page is the owner's own; a missing
    // value would read as a chat session on a second site (helm.store.normalize_by).
    if (isWork() && (verb === "reply" || verb === "close")) body = { ...body, by: "owner" };
    // A Use-the-draft press carries the header the web role requires of it: the confirm sheet sends it, a
    // forged cross-site request cannot (docs/API.md, Who can press).
    const press = verb === "reply" && typeof body.value === "string" && /^\s*__use_draft__:/i.test(body.value);
    const r = await api(`/api/triage/threads/${encodeURIComponent(t.thread_id)}/${verb}`,
      { method: "POST", body: JSON.stringify(body), ...(press ? { headers: { "Content-Type": "application/json", "X-Helm-Press": "confirmed" } } : {}) });
    if (r && r.accepted === false) throw new Error(r.ignored || JSON.stringify(r));
    if (verb === "close" && Array.isArray(r.writeback) && r.writeback.some(w => !w.ok))
      toast("Closed, but the tracker write-back failed — recorded on the thread", true);
    else toast(okMsg);
    delete drafts[t.thread_id + ":note"]; saveDrafts();
    if (typeof Helm !== "undefined") Helm.emit("acted", { thread_id: t.thread_id, verb, result: r });
    await load();
    return r;
  } catch (e) { toast(`${verb} didn't send: ${e.message}`, true); if (node) node.classList.remove("busy"); return null; }
  finally { acting = false; }
}

// ---------- file a card ----------
function renderPicks() {
  const urow = $("#c-urgency-row");
  urow.replaceChildren(...URGENCIES.map(([value, label]) => {
    const b = el("button", "chip" + (sheetPick.urgency === value ? " on" : ""), label);
    b.type = "button";
    b.onclick = () => { sheetPick.urgency = value; drafts["c-urgency"] = value; saveDrafts(); renderPicks(); };
    return b;
  }));
  // Who the card is for: a pick where the deployment offers one, else the default.
  const irow = $("#c-intent-row");
  const intent = isWork() ? sheetPick.intent : "agent";
  irow.replaceChildren(...INTENTS.map(([value, label]) => {
    const b = el("button", "chip" + (intent === value ? " on" : ""), label);
    b.type = "button";
    b.onclick = () => { sheetPick.intent = value; drafts["c-intent"] = value; saveDrafts(); renderPicks(); };
    return b;
  }));
  $("#c-hint").textContent = INTENT_HINT[intent];
  $("#c-blocked").classList.toggle("hidden", intent !== "waiting");
  $("#c-details").placeholder = intent === "agent" ? "Details — the brief for the run (optional; the title is the brief when empty)" : "Details (optional)";
}
// `prefill` comes from the notebook ("make this a thread" on a carry-over line).
function openSheet(prefill) {
  sheetPick.urgency = URGENCIES.some(([v]) => v === drafts["c-urgency"]) ? drafts["c-urgency"] : "whenever";
  sheetPick.intent = INTENTS.some(([v]) => v === drafts["c-intent"]) ? drafts["c-intent"] : "agent";
  if (prefill && prefill.intent) sheetPick.intent = prefill.intent;
  renderPicks();
  $("#c-title").value = (prefill && prefill.title) ?? drafts["c-title"] ?? "";
  $("#c-details").value = (prefill && prefill.summary) ?? drafts["c-details"] ?? "";
  $("#c-link").value = (prefill && prefill.link) ?? drafts["c-link"] ?? "";
  $("#c-category").value = drafts["c-category"] ?? "";
  const dl = $("#c-cats");
  dl.replaceChildren(...[...new Set(state.threads.filter(t => t.state !== "done").map(t => (t.category || "").trim()).filter(Boolean))].sort().map(c => { const o = el("option"); o.value = c; return o; }));
  $("#sheet").classList.remove("hidden");
  // Only with a mouse on a wide screen: on a phone or touch screen the focus opens the keyboard
  // over the timing and folder pickers (the owner 2026-09-09; helm-web v13 dropped it, ).
  if (matchMedia("(pointer: fine) and (min-width: 761px)").matches) $("#c-title").focus();
}
function closeSheet() { $("#sheet").classList.add("hidden"); }
// A card the owner types here is work for an agent, never a chore handed back to him
// (the sheet has said so since v1: "Lands under IN FLIGHT for an agent"). The
// store defaults `intent` to "mine", which files it lit with "Is this finished?"
// — the exact hand-back this sheet promises not to do — so the intent is sent
// explicitly. `instruction` is what the dispatcher acts on and is required for
// intent "agent"; a title-only card is a legitimate brief, so it falls back to
// the title. The detail box is `summary`: the store has no `details` key, and
// sending one silently dropped every word typed into it (fixed 2026-09-18).
// Where the deployment offers intents, the signer picks one.
async function submitCreate() {
  const title = $("#c-title").value.trim(); if (!title) return toast("Title required", true);
  if (state.offline) return toast("gateway unreachable — not sent", true);
  const btn = $("#c-file"); btn.disabled = true;
  const details = $("#c-details").value.trim();
  const intent = isWork() ? sheetPick.intent : "agent";
  const body = { title, summary: details, urgency: sheetPick.urgency, intent };
  if (intent === "agent") body.instruction = details || title;
  if (isWork()) {
    const cat = $("#c-category").value.trim(); if (cat) body.category = cat;
    const link = $("#c-link").value.trim();
    if (link) { const rel = noteRelFromUrl(link); body.links = [{ label: rel ? rel.replace(/\.md$/i, "").split("/").pop() : link, url: link }]; }
    if (intent === "waiting") { const bo = $("#c-blocked").value.trim(); if (bo) body.blocked_on = bo; }
  }
  try {
    const r = await api("/api/triage/threads/create", { method: "POST", body: JSON.stringify(body) });
    for (const k of ["c-title", "c-details", "c-urgency", "c-category", "c-intent", "c-link"]) delete drafts[k];
    saveDrafts();
    closeSheet();
    toast(r.handed_off ? `Filed ${r.thread_id} — handed to an agent`
      : intent === "waiting" ? `Filed ${r.thread_id} — waiting on someone else`
      : intent === "mine" ? `Filed ${r.thread_id} — it's in NEEDS YOU`
      : `Filed ${r.thread_id} — it's in IN FLIGHT`);
    await load();
    // The new card opens as its conversation (every card is a conversation).
    if (typeof Helm !== "undefined" && r.thread_id) Helm.go(Helm.convHref(r.thread_id, intent === "mine" ? "needs-you" : "threads"));
  } catch (e) { toast(`didn't file: ${e.message}`, true); } finally { btn.disabled = false; }
}
// A thread can name the note it is about: a note: scheme, or the note's path under the
// vault or a folder the vault used to live in (Helm.vaults, from /api/notes/meta; an old card
// cites its minutes by the client path). The same rule as lib.js noteRelFromUrl.
function noteRelFromUrl(url) {
  const u = String(url || "").trim();
  if (/^note:/i.test(u)) {
    let rel = u.slice(5).trim().replace(/\\/g, "/").replace(/^\/+/, "");
    if (!/\.md$/i.test(rel)) rel += ".md";
    return rel;
  }
  const n = u.replace(/\\/g, "/");
  if (!/\.md$/i.test(n) || typeof Helm === "undefined" || !Helm.vaults) return null;
  for (const v of Helm.vaults) {
    const p = String(v).replace(/\\/g, "/").toLowerCase().replace(/\/+$/, "") + "/";
    if (p !== "/" && n.toLowerCase().startsWith(p)) return n.slice(p.length);
  }
  return null;
}

// ---------- helpers ----------
function kindLabel(k) { return { "bot-result": "Agent report", "owner-reply": "You chose", "owner-note": "Your note", "state-change": "State", filed: "Filed" }[k] || k; }
function cleanSummary(s) {
  if (!s) return "";
  const str = String(s).trim();
  const m = str.match(/^(.*?)(Needs you because:\s*.*)$/s);
  if (m && m[1].trim()) {
    return `${m[2].trim()}\n\n${m[1].trim()}`;
  }
  return str;
}
function fmtTimeET(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false }) + " ET";
}
// A markdown image may load only from this site (resolved), a blob: URL or a data:image/ URL: a prompt-injected
// reply must not make the browser fetch an outside address that carries chat text (finding 5). Everything else is
// plain text or, when the scheme is linkable, a link — never an <img>.
function imageSrcAllowed(url) {
  const u = String(url || "").trim();
  if (/^data:image\//i.test(u)) return true;
  try {
    const parsed = new URL(u, location.href);
    return parsed.origin === location.origin || parsed.protocol === "blob:";
  } catch (e) {
    return false;
  }
}
function openLightbox(src, caption) {
  const lb = $("#lightbox");
  if (!lb) return;
  if (!imageSrcAllowed(src)) return;         // the lightbox never fetches a disallowed source either
  const img = $("#lightbox-img");
  const cap = $("#lightbox-caption");
  if (img) img.src = src;
  if (cap) cap.textContent = caption || "";
  lb.classList.remove("hidden");
}
function closeLightbox() {
  const lb = $("#lightbox");
  if (lb) lb.classList.add("hidden");
}

function renderBody(text) {
  if (!text) return document.createDocumentFragment();
  const frag = document.createDocumentFragment();
  let body = String(text);

  const m = body.match(/^\[([^\]]{1,40})\]\s*/);
  if (m) {
    frag.append(el("span", "pill", m[1]), " ");
    body = body.slice(m[0].length);
  }

  // Split by code blocks ```...```
  const codeBlockRegex = /```(?:([a-zA-Z0-9_-]+)\n)?([\s\S]*?)```/g;
  let lastIdx = 0;
  let match;
  while ((match = codeBlockRegex.exec(body)) !== null) {
    if (match.index > lastIdx) {
      renderInlineMarkdown(body.slice(lastIdx, match.index), frag);
    }
    const pre = el("pre", "code-block");
    pre.textContent = match[2];
    frag.append(pre);
    lastIdx = codeBlockRegex.lastIndex;
  }
  if (lastIdx < body.length) {
    renderInlineMarkdown(body.slice(lastIdx), frag);
  }
  return frag;
}

// A link opens only an http(s) or mailto address (a relative one resolves to this site's):
// never javascript:, data: or any other scheme. The browser's own parse decides, so
// "java\tscript:" is caught the way the browser would read it.
function safeLinkHref(url) {
  const u = String(url || "").trim();
  try {
    return ["http:", "https:", "mailto:"].includes(new URL(u, location.href).protocol) ? u : null;
  } catch (e) {
    return null;
  }
}
function bodyLink(label, href) {
  const a = el("a", "body-link", label);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  return a;
}
// A bare address ends before trailing sentence punctuation and before a ) ] } it did not open,
// so "see https://x/y." and "(https://x/y)" link just the address.
function splitBareUrl(p) {
  let url = p;
  const closer = { ")": "(", "]": "[", "}": "{" };
  for (;;) {
    const c = url.slice(-1);
    if (".,;:!?*".includes(c)) { url = url.slice(0, -1); continue; }
    if (closer[c] && url.split(closer[c]).length < url.split(c).length) { url = url.slice(0, -1); continue; }
    break;
  }
  return [url, p.slice(url.length)];
}
function appendBareUrl(p, parent) {
  const [url, rest] = splitBareUrl(p);
  if (/^https?:\/\/[^/?#\s]/i.test(url)) parent.append(bodyLink(url, url));
  else parent.append(document.createTextNode(url));
  if (rest) parent.append(document.createTextNode(rest));
}
// Plain text with its bare http(s) addresses linked and nothing else interpreted.
function renderLinkedText(text) {
  const frag = document.createDocumentFragment();
  for (const p of String(text || "").split(/(https?:\/\/[^\s<>"'`]+)/g)) {
    if (!p) continue;
    if (/^https?:\/\//i.test(p)) appendBareUrl(p, frag);
    else frag.append(document.createTextNode(p));
  }
  return frag;
}

// In-app references in plain prose (never inside `code` or a ``` block: those callers do not pass through here):
//   a Helm thread id he has (t-...) -> that conversation
//   [[Notebook page]] or [[Page|label]] -> the page in the NOTEBOOK     (the notebook module only)
// Anything else, and any id that is not known here, stays text.
const REF_RE = /(\[\[[^\[\]\n|]{1,200}(?:\|[^\[\]\n]{1,120})?\]\]|\bt_[0-9a-f]{8}\b|\bt-[a-z0-9][a-z0-9-]{2,80}[a-z0-9]\b)/g;
// A reference is an object chip (web/objects.js): the type's glyph and colour, and a peek on hover or long-press. Without
// objects.js (a page that never loaded it) it falls back to the plain link it always was.
function refAnchor(label, href, title, spec) {
  if (spec && typeof Helm !== "undefined" && typeof Helm.chip === "function") {
    const a = Helm.chip(Object.assign({ label, href }, spec));
    a.classList.add("body-link", "body-ref");
    return a;
  }
  const a = el("a", "body-link body-ref", label);
  a.href = href; if (title) a.title = title;
  return a;
}
function appendRefText(text, parent) {
  text = String(text == null ? "" : text);
  const H = typeof Helm !== "undefined" ? Helm : null;
  const has = (m) => !!(H && typeof H.has === "function" && H.has(m));
  const parts = text.split(REF_RE);
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (!p) continue;
    if (i % 2 === 0) { parent.append(document.createTextNode(p)); continue; }
    let a = null;
    if (p.startsWith("[[")) {
      if (has("notebook") && H.noteHref) {
        const inner = p.slice(2, -2), bar = inner.indexOf("|");
        const page = (bar < 0 ? inner : inner.slice(0, bar)).trim(), label = bar < 0 ? page : inner.slice(bar + 1).trim();
        const rel = /\.md$/i.test(page) ? page : page + ".md";
        const day = /^(?:.*\/)?\d{4}-\d{2}-\d{2}\.md$/.test(rel);
        if (page) a = refAnchor(label || page, H.noteHref(rel), "Open the notebook page " + page, { type: day ? "day" : "note", id: rel });
      }
    } else if (typeof threadById === "function" && threadById(p) && H && H.convHref) {
      const t = threadById(p);
      a = refAnchor(p, H.convHref(p), "Open this conversation", { type: t && t.state === "needs-you" ? "ask" : "thread", id: p });
    }
    parent.append(a || document.createTextNode(p));
  }
}
window.appendRefText = appendRefText;

function renderInlineMarkdown(text, parent) {
  const tokenRegex = /(!?\[[^\]]*\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|https?:\/\/[^\s<>"'`]+|\n)/g;
  const parts = text.split(tokenRegex);
  for (const p of parts) {
    if (!p) continue;
    if (/^https?:\/\//i.test(p)) {
      appendBareUrl(p, parent);
    } else if (p === "\n") {
      parent.append(document.createElement("br"));
    } else if (p.startsWith("![") && p.includes("](") && p.endsWith(")")) {
      const altMatch = p.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
      if (altMatch && imageSrcAllowed(altMatch[2])) {
        const img = document.createElement("img");
        img.src = altMatch[2];
        img.alt = altMatch[1] || "Visual";
        img.className = "body-inline-img";
        img.loading = "lazy";
        img.onclick = () => openLightbox(altMatch[2], altMatch[1]);
        parent.append(img);
      } else if (altMatch) {
        const label = altMatch[1] || "Image";
        const href = safeLinkHref(altMatch[2]);
        if (href) parent.append(bodyLink(label, href));
        else parent.append(document.createTextNode(label));
      } else {
        parent.append(document.createTextNode(p));
      }
    } else if (p.startsWith("[") && p.includes("](") && p.endsWith(")")) {
      const linkMatch = p.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = linkMatch && safeLinkHref(linkMatch[2]);
      if (href) {
        parent.append(bodyLink(linkMatch[1], href));
      } else {
        parent.append(document.createTextNode(p));
      }
    } else if (p.startsWith("**") && p.endsWith("**")) {
      parent.append(el("strong", null, p.slice(2, -2)));
    } else if (p.startsWith("`") && p.endsWith("`")) {
      parent.append(el("code", null, p.slice(1, -1)));
    } else {
      appendRefText(p, parent);
    }
  }
}
function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""; }
function fmtAge(ts, future = false) {
  if (!ts) return ""; const s = future ? ts - Date.now() / 1000 : Date.now() / 1000 - ts;
  if (s < 0 && future) return "now"; const a = Math.abs(s); const sfx = future ? "" : " ago"; const pfx = future ? "in " : "";
  if (a < 60) return future ? "now" : "just now";
  if (a < 3600) return `${pfx}${Math.floor(a / 60)}m${sfx}`;
  if (a < 86400) return `${pfx}${Math.floor(a / 3600)}h${sfx}`;
  return `${pfx}${Math.floor(a / 86400)}d${sfx}`;
}
// How old the question on a card is. Read from the store's absolute times (thread.catchup), never from
// anything an agent wrote: a card cannot claim it was asked this morning. last_change is only the fallback
// for a store that predates catchup, because it moves on any touch and made a two-week-old card look new.
// lit_at counts only while the card is lit for him: an answered thread keeps its ask columns, and its age is when he answered.
function askedAt(t) { const c = t && t.catchup; return (t && t.state === "needs-you" && c && c.lit_at) || (t && t.last_change) || 0; }
function catchupChips(t) {
  const c = (t && t.catchup) || {}, out = [], lit = c.lit_at || 0;
  if (!lit) return out;
  const age = Date.now() / 1000 - lit;
  if (c.subject) out.push({ cls: "conv-cu-subject", text: c.subject });
  out.push({ cls: age > 7 * 86400 ? "conv-cu-stale" : age > 86400 ? "conv-cu-old" : "", text: "asked " + fmtAge(lit), title: new Date(lit * 1000).toLocaleString() });
  if ((c.times_asked || 0) > 1) out.push({ cls: "", text: "asked " + c.times_asked + " times" });
  if ((c.reports_since || 0) > 0) out.push({ cls: "", text: c.reports_since + (c.reports_since === 1 ? " update" : " updates") + " since, last " + fmtAge(c.checked_at) });
  if (c.owner_last_at) out.push({ cls: "", text: "you last wrote " + fmtAge(c.owner_last_at), title: new Date(c.owner_last_at * 1000).toLocaleString() });
  return out;
}
let toastTimer;
function toast(msg, err = false) {
  document.querySelectorAll(".toast").forEach(x => x.remove());
  const t = el("div", "toast" + (err ? " err" : ""), msg); document.body.append(t);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.remove(), err ? 6000 : 2500);
}
function saveCats() { try { localStorage.setItem("helm.cats", JSON.stringify([...state.categories])); } catch { /* per-viewer convenience */ } }

// ---------- wiring ----------
$("#add").onclick = () => openSheet();
$("#c-cancel").onclick = closeSheet;
const closeX = $("#c-close-x"); if (closeX) closeX.onclick = closeSheet;
markSubmit($("#c-file"), submitCreate);

const lbClose = $("#lightbox-close");
if (lbClose) lbClose.onclick = closeLightbox;
const lbElem = $("#lightbox");
if (lbElem) {
  lbElem.addEventListener("click", (e) => {
    if (e.target.id === "lightbox" || e.target.classList.contains("lightbox-overlay")) closeLightbox();
  });
}

$("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(); });
// An earlier build filed on Ctrl/Cmd+Enter from any field and on Enter in the title
// . Both go through File it's submit gate, so a quick second press files nothing.
// Enter in the details stays a new line; a focused button keeps the gate's own Enter.
$("#sheet").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.repeat || e.isComposing || e.altKey || e.shiftKey) return;
  if (e.target.closest && e.target.closest("button")) return;
  if (!(e.ctrlKey || e.metaKey) && e.target.id !== "c-title") return;
  e.preventDefault();
  const b = $("#c-file"); if (!b.disabled && b._act) b._act();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeSheet();
    closeLightbox();
  }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
window.addEventListener("online", () => load());
for (const id of ["c-title", "c-details", "c-category", "c-link"]) if (typeof drafts[id] === "string") $("#" + id).value = drafts[id];
setInterval(() => { if (!document.hidden) load(); }, 60_000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
load();
