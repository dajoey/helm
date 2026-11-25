// The compact thread card, for the notebook's own contexts: the topic hub
// beside a note ("threads about this") and the day page. The inbox and search draw
// helm-web's card (app.js card()); a thread appears once on any screen. Every action is
// a POST to the store's own routes through the web role, with the store's answer,
// rejections included, shown word for word. The 💬 opens the conversation overlay.

import {api, esc, richText, age, ageClass, toast, open, histOpen, saveOpen,
        noteHref, noteRelFromUrl, optWords, threadHref, appName} from "./lib.js";

const STATE_LABEL = {"needs-you":"needs you", "working":"answered",
                     "waiting-external":"waiting", "backlog":"backlog", "done":"done"};

export function chipInfo(t){
  if(t.state === "waiting-external" && t.parked) return {cls: "parked", label: "parked"};
  return {cls: t.state, label: STATE_LABEL[t.state] || t.state};
}

// A thread can name the page it is about. A web address is a link, a note:
// url or a path inside the vault opens the note here, another thread's id opens
// that thread, anything else renders as visible text because it is a path to
// open by hand. t.refs is the notebook's answer (topic.refs), which spells a
// note as it is held.
export function linksHtml(t){
  const links = t.links || [];
  if(!links.length) return "";
  const refs = t.refs || {};
  const rows = links.map(l => {
    const url = l.url || "", label = l.label || url;
    const ref = refs[String(url).trim()] || {};
    const rel = ref.note || noteRelFromUrl(url);
    if(ref.thread && !rel){
      return '<div class="linkrow"><a href="' + threadHref(ref.thread) + '" title="Open that thread">' + esc(label) + '</a></div>';
    }
    if(rel){
      return '<div class="linkrow is-note"><a href="' + noteHref(rel) + '" title="' + esc(rel) + '">' +
             esc(label !== url ? label : rel) + '</a></div>';
    }
    if(/^https?:\/\//i.test(url)){
      return '<div class="linkrow"><a href="' + esc(url) + '" target="_blank" rel="noopener">' +
             esc(label) + '</a></div>';
    }
    const shown = label !== url ? esc(label) + ': ' + esc(url) : esc(url);
    return '<div class="linkrow"><code>' + shown + '</code></div>';
  }).join("");
  return '<div class="links">' + rows + '</div>';
}

export function optionsHtml(t){
  const a = t.ask || {};
  if(a.kind === "choice"){
    return (a.options || []).map(o =>
      '<button class="opt' + (o.terminal ? ' term' : '') + '" data-act="reply" data-id="' +
      esc(t.thread_id) + '" data-val="' + esc(o.value) + '"' +
      (o.terminal ? ' data-terminal="1"' : '') + '>' +
      '<span class="lab">' + esc(o.label) + '</span>' +
      (o.terminal ? '<span class="tag">settles it</span>' : '') +
      (o.effect ? '<div class="eff">' + esc(o.effect) + '</div>' : '') + '</button>').join("");
  }
  if(a.kind === "ack"){
    return '<button class="opt" data-act="reply" data-id="' + esc(t.thread_id) +
           '" data-val="ack"><span class="lab">Got it</span></button>';
  }
  if(a.kind === "text"){
    return '<textarea rows="3" id="txt-' + esc(t.thread_id) +
           '" placeholder="your answer"></textarea>' +
           '<div class="acts"><button class="send" data-act="replytext" data-id="' + esc(t.thread_id) +
           '">Send answer</button></div>';
  }
  return '<div class="warn">This thread is lit but carries no question, which should not ' +
         'be possible. Its history is above; use Close or Mute.</div>';
}

// The answered line under a card's chip row. Helm clears the options when a
// thread is answered, so when the label is gone the option's id is shown as words.
function answerOf(t){
  const evs = (t.events || []).filter(e => e.kind === "owner-reply");
  if(!evs.length) return null;
  const last = evs[evs.length - 1];
  const v = last.body;
  const note = (t.last_note || "").trim();
  if(v === "ack") return {verb: "you acknowledged it", label: "", ts: last.ts, note};
  if(v === "__redirect__") return {verb: "you answered with a note", label: "", ts: last.ts, note: ""};
  if(v === "__task__") return {verb: "you handed it over", label: "", ts: last.ts, note};
  const opt = ((t.ask || {}).options || []).find(o => o.value === v);
  return {verb: "you answered", label: opt ? opt.label : optWords(v), raw: !opt, ts: last.ts, note};
}

// A history entry's record type, in words.
const EV_LABEL = {
  "owner-reply": "you answered", "owner-note": "your note", "bot-result": "report",
  "state-change": "state", "update": "producer update", "failure-detail": "failure detail",
  "rule-breach": "rule breach", "log-tail": "log tail", "chat-note": "chat note",
  "park": "parked", "unpark": "unparked", "backlog": "to the backlog", "unbacklog": "pulled back",
};
function evLabel(k){ return EV_LABEL[k] || String(k || "").replace(/-/g, " "); }

function evHtml(e){
  const body = e.kind === "owner-reply" ? optWords(e.body)
    : (typeof window.Helm !== "undefined" && window.Helm.humanBody) ? window.Helm.humanBody(e.kind, e.body) : e.body;
  return '<div class="ev ' + esc(e.kind) + '"><div class="who">' + esc(evLabel(e.kind)) + ' &middot; ' +
         esc(age(e.ts)) + '</div><div class="body">' + richText(body) + '</div></div>';
}

export function threadHtml(t, showCat){
  const isOpen = open.has(t.thread_id);
  const ans = answerOf(t);
  const settled = t.state !== "needs-you";
  let h = '<div class="thread' + (settled ? ' settled' : ' lit') + ' ' + esc(t.urgency) +
          (isOpen ? ' isopen' : '') + (sending.has(t.thread_id) ? ' sending' : '') +
          '" id="t-' + esc(t.thread_id) + '">';
  const chip = chipInfo(t);
  h += '<div class="trow" data-act="toggle" data-id="' + esc(t.thread_id) + '">' +
       '<span class="chip ' + esc(chip.cls) + '">' + esc(chip.label) + '</span>' +
       '<span class="dot ' + esc(t.urgency) + '" title="' +
       esc(t.urgency === "this-week" ? "this week" : t.urgency) + '"></span>' +
       (showCat ? '<span class="cat">' + esc(t.category || "Uncategorized") + '</span>' : '') +
       (t.match === "suggested" ? '<span class="sugg" title="' + esc(t.match_why || "") + '">suggested</span>' : '') +
       '<span class="age' + ageClass(t) + '" title="last change">' + esc(age(t.last_change)) + '</span>';
  if(ans && t.state !== "needs-you"){
    h += '<div class="answered">' + esc(ans.verb) +
         (ans.label ? ' <b' + (ans.raw ? ' title="the option as ' + appName() + ' recorded it; its wording was not kept"' : '') + '>' + esc(ans.label) + '</b>' : '') +
         ' <span class="whenago">' + esc(age(ans.ts)) + '</span>' +
         (ans.note ? ' &middot; <span class="whenago">with a note</span>' : '') + '</div>';
  }
  h += '</div>';
  h += '<div class="ttitle" data-act="toggle" data-id="' + esc(t.thread_id) + '">' +
       esc(t.title) + ' <a class="tconv" href="' + threadHref(t.thread_id) + '" title="Open the conversation">💬</a></div>';
  if(!isOpen){
    const peek = String(t.summary || "").replace(/\s+/g, " ").trim();
    if(peek) h += '<div class="peek" data-act="toggle" data-id="' + esc(t.thread_id) + '">' +
                  esc(peek) + '</div>';
  }
  h += linksHtml(t);
  if(!isOpen) return h + '</div>';

  h += '<div class="summary">' + richText(t.summary) + '</div>';

  const evs = (t.events || []).filter(e => e.body);
  let lastReport = -1;
  for(let i = evs.length - 1; i >= 0; i--){ if(evs[i].kind === "bot-result"){ lastReport = i; break; } }
  if(lastReport >= 0){
    const e = evs[lastReport];
    h += '<div class="report"><div class="who">latest report &middot; ' + esc(age(e.ts)) +
         '</div><div class="body">' + richText(e.body) + '</div></div>';
  }
  const rest = evs.filter((e, i) => i !== lastReport);
  if(rest.length){
    const unfolded = histOpen.has(t.thread_id);
    h += '<div class="hist"><button class="htog" data-act="hist" data-id="' + esc(t.thread_id) +
         '">' + (unfolded ? '&#9662; ' : '&#9656; ') + 'history &middot; ' + rest.length +
         (lastReport >= 0 ? ' earlier' : '') + '</button>';
    if(unfolded) for(const e of rest) h += evHtml(e);
    h += '</div>';
  }

  if(t.ask && t.ask.prompt) h += '<div class="prompt">' + esc(t.ask.prompt) + '</div>';
  if(t.state === "needs-you"){
    h += optionsHtml(t);
  }else if(t.state === "working"){
    h += '<div class="note-state ok">Answered. Something is acting on this; the report will ' +
         'land in the history above. You can still close it, mute it, or answer again to ' +
         'change course.</div>';
  }else if(t.state === "waiting-external" && t.parked){
    h += '<div class="note-state parked"><b>Parked.</b> ' + esc(t.parked_reason || "") + '</div>' +
         (isWork() ? '<div class="acts"><button data-act="unpark" data-id="' + esc(t.thread_id) +
         '">unpark, this is moving again</button></div>' : '');
  }else if(t.state === "waiting-external"){
    h += '<div class="note-state">Waiting on someone else. Nothing is needed from you.' +
         (t.blocked_on ? ' <span class="muted">Waiting on: ' + esc(t.blocked_on) + '</span>' : '') + '</div>' +
         (isWork() ? '<div class="acts"><button data-act="park" data-id="' + esc(t.thread_id) +
         '">park this, nothing is actually pushing it</button></div>' : '');
  }else if(t.state === "backlog"){
    h += '<div class="note-state"><b>In the backlog.</b> It keeps its question and comes back ' +
         'only when you pull it.</div><div class="acts"><button data-act="unbacklog" data-id="' +
         esc(t.thread_id) + '">pull it back, this is now</button></div>';
  }else if(t.state === "done"){
    h += '<div class="note-state">Settled. Reopening asks fresh rather than guessing back ' +
         'the original options.</div><div class="acts"><button data-act="reopen" data-id="' +
         esc(t.thread_id) + '">reopen</button></div>';
  }

  h += '<div class="notebox"><div class="nlab"><b>note</b>' +
       '<span>rides with whatever you pick and wins over the option text, or send it alone</span></div>';
  h += '<textarea rows="2" id="note-' + esc(t.thread_id) +
       '" placeholder="what you want done"></textarea>';
  h += '<div class="acts">' +
       '<button class="send" data-act="redirect" data-id="' + esc(t.thread_id) + '" ' +
       'title="Ctrl+Enter from the note does this">none of these, do what my note says</button>' +
       '<span class="grow"></span>' +
       '<button class="quiet" data-act="snooze" data-id="' + esc(t.thread_id) + '" data-days="1">snooze 1d</button>' +
       '<button class="quiet" data-act="snooze" data-id="' + esc(t.thread_id) + '" data-days="7">snooze 1w</button>' +
       '<span class="until"><input type="date" id="sn-' + esc(t.thread_id) + '" title="snooze until this date">' +
       '<button class="quiet" data-act="snoozeuntil" data-id="' + esc(t.thread_id) + '">until</button></span>' +
       (t.state === "needs-you" && isWork() ? '<button class="quiet" data-act="backlog" data-id="' + esc(t.thread_id) + '" title="Not now. Needs a line in the note saying why.">backlog</button>' : '') +
       '<button class="quiet" data-act="close" data-id="' + esc(t.thread_id) + '">close</button>' +
       '<button class="quiet" data-act="mute" data-id="' + esc(t.thread_id) + '">mute this kind</button>' +
       '</div></div>';

  if((t.sources || []).length){
    // A source naming a note (a call's minutes) or another thread links to it.
    const refs = t.refs || {};
    h += '<div class="src">from ' + t.sources.filter(s => s != null && s !== "").map(s => {
      const o = typeof s === "object" ? s : {id: String(s)};
      const text = esc(((o.source || "") + " " + (o.id || o.note || "")).trim());
      const ref = o.id != null ? refs[String(o.id).trim()] : null;
      if(ref && ref.note) return '<a href="' + noteHref(ref.note) + '" title="' + esc(ref.note) + '">' + text + '</a>';
      if(ref && ref.thread) return '<a href="' + threadHref(ref.thread) + '" title="Open that thread">' + text + '</a>';
      return text;
    }).join(", ") + '</div>';
  }
  return h + '</div>';
}

// Every rebuild of a region snapshots what is typed inside it and puts it
// back after, keyed by element id, so a refresh can never eat a note.
export function snapshotInputs(roots){
  const snap = {values: {}, scrollY: window.scrollY};
  for(const root of roots){
    if(!root) continue;
    root.querySelectorAll("textarea, input[type=date], input[type=text], input[type=search]").forEach(el => {
      if(!el.id || !el.value) return;
      snap.values[el.id] = {value: el.value, focused: el === document.activeElement,
                            selStart: el.selectionStart, selEnd: el.selectionEnd};
    });
  }
  return snap;
}
export function restoreInputs(snap){
  for(const id in snap.values){
    const el = document.getElementById(id);
    if(!el) continue;
    const v = snap.values[id];
    el.value = v.value;
    if(v.focused){
      el.focus();
      try{ el.setSelectionRange(v.selStart, v.selEnd); }catch(e){}
    }
  }
  window.scrollTo(0, snap.scrollY);
}

function confirmIfNeeded(el, a){
  const threadEl = el.closest(".thread");
  const title = threadEl ? (threadEl.querySelector(".ttitle") || {}).textContent || "" : "";
  if(a === "reply" && el.dataset.terminal === "1"){
    const lab = (el.querySelector(".lab") || {}).textContent || el.dataset.val;
    const eff = (el.querySelector(".eff") || {}).textContent || "";
    return confirm("Settle as done:\n" + title + "\n\nYou picked: " + lab +
                   (eff ? "\n" + eff : "") +
                   "\n\nYou can reopen it after, but you will be asked fresh: " +
                   "these options will not come back.");
  }
  if(a === "close"){
    return confirm("Close as done:\n" + title + "\n\nYou can reopen it after, but you will be asked fresh.");
  }
  if(a === "mute"){
    return confirm("Mute, and stop showing anything like it, going forward:\n" + title +
                   "\n\nThere is no undo for this one. A muted thread does not come back " +
                   "on the list to reopen.");
  }
  return true;
}

// The store's routes (the same ones the inbox uses), so the notebook's card never depends
// on a second door. At work a tap here is the owner's own (`by: "owner"`, a rule pack).
const isWork = () => typeof window.Helm !== "undefined" && window.Helm.site === "work";
const post = (id, action, body) => {
  if(isWork() && (action === "reply" || action === "close")) body = Object.assign({by: "owner"}, body);
  return api("/api/triage/threads/" + encodeURIComponent(id) + "/" + action, {method: "POST", body: JSON.stringify(body)});
};

// Threads with an action on its way to Helm. A second tap on the same card is
// refused until the first one has an answer.
const sending = new Set();

function lockCard(id, el, on){
  const threadEl = document.getElementById("t-" + id);
  if(!threadEl) return;
  threadEl.classList.toggle("sending", on);
  threadEl.querySelectorAll("button").forEach(b => { if(b.dataset.act !== "toggle") b.disabled = on; });
  if(el && el.classList) el.classList.toggle("busy", on);
}

// Returns true when the element was a card action (handled here), false when
// it belongs to somebody else. `rerender` redraws without a network round
// trip; `refresh` re-reads from Helm after an action that changed a thread.
export async function cardAction(el, rerender, refresh){
  const id = el.dataset.id;
  const a = el.dataset.act;
  if(a === "toggle"){
    open.has(id) ? open.delete(id) : open.add(id);
    saveOpen(); rerender(); return true;
  }
  if(a === "hist"){
    histOpen.has(id) ? histOpen.delete(id) : histOpen.add(id);
    rerender(); return true;
  }
  const known = ["reply","replytext","redirect","close","snooze","snoozeuntil","mute","park",
                 "unpark","backlog","unbacklog","reopen"];
  if(!known.includes(a)) return false;
  if(sending.has(id)){ toast("still sending your last answer on this card"); return true; }
  if(!confirmIfNeeded(el, a)) return true;
  const noteEl = document.getElementById("note-" + id);
  const note = noteEl ? noteEl.value.trim() : "";
  let req = null, done = "";
  if(a === "reply"){
    req = ["reply", {value: el.dataset.val, note: note}];
    done = "answered: " + el.dataset.val + (note ? " (with note)" : "");
  }else if(a === "replytext"){
    const v = (document.getElementById("txt-" + id) || {}).value || "";
    if(!v.trim()){ toast("type an answer first"); return true; }
    req = ["reply", {value: v.trim(), note: note}]; done = "answered";
  }else if(a === "redirect"){
    if(!note){ toast("type what you want done in the note first"); if(noteEl) noteEl.focus(); return true; }
    req = ["reply", {value: "__redirect__", note: note}]; done = "sent your instruction";
  }else if(a === "close"){
    req = ["close", {reason: note}]; done = "closed";
  }else if(a === "backlog" && !isWork()){
    toast("the backlog is a work-site affordance"); return true;
  }else if(a === "snooze"){
    const days = parseFloat(el.dataset.days || "1");
    req = ["snooze", {wake_at: Date.now()/1000 + days*86400, note: note}];
    done = "hidden for " + days + " day" + (days > 1 ? "s" : "");
  }else if(a === "snoozeuntil"){
    const d = (document.getElementById("sn-" + id) || {}).value;
    if(!d){ toast("pick a date first"); return true; }
    const wake = new Date(d + "T09:00:00");
    req = ["snooze", {wake_at: wake.getTime()/1000, note: note}]; done = "hidden until " + d;
  }else if(a === "mute"){
    req = ["mute", {note: note}]; done = "muted";
  }else if(a === "park"){
    if(!note){ toast("type why in the note first, that becomes the parked reason"); if(noteEl) noteEl.focus(); return true; }
    req = ["park", {reason: note}]; done = "parked";
  }else if(a === "unpark"){
    req = ["unpark", {note: note}]; done = "unparked, back in flight";
  }else if(a === "backlog"){
    if(!note){ toast("type a line in the note first: what this is and why it is not now"); if(noteEl) noteEl.focus(); return true; }
    req = ["backlog", {reason: note}]; done = "in the backlog";
  }else if(a === "unbacklog"){
    req = ["unbacklog", {note: note}]; done = "pulled back, it needs you again";
  }else if(a === "reopen"){
    req = ["reopen", {note: note}]; done = "reopened, ask again below";
  }
  sending.add(id);
  lockCard(id, el, true);
  try{
    const r = await post(id, req[0], req[1]);
    if(a === "mute" && r.subject) done = "muted: “" + r.subject + "”";
    if(r.slow) toast(done + ". " + appName() + " was slow to answer, but it has it.", 5000);
    else toast(done);
    open.delete(id); saveOpen();
    await refresh();
  }catch(e){
    if((e.payload || {}).helm_status === 504){
      toast(e.message, 8000);
      await refresh();
    }else{
      toast(appName() + " said no: " + e.message);
    }
  }finally{
    sending.delete(id);
    lockCard(id, el, false);
  }
  return true;
}
