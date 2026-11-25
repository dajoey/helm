/* The conversation. Every thread,
   from every place it appears, opens here in the middle pane with its properties in the
   right pane: one ordered timeline of the owner's messages, the agent's reports and system events
   (/api/conversations/{id}, the thread's own events, or the read API when that does not
   answer), an SSE stream that updates it without a refresh, "Jump to latest" when scrolled
   up, and a composer that works in every state:
     needs-you  -> the ask's options, context and how-to; a note with the answer, files,
                   "send note only", snooze, "don't ask again", backlog, close on a
                   card with no ask — everything helm-web's card offered (L10);
     working, waiting -> a note is an answer in his words and changes course;
     done, backlog -> a note reopens or pulls it back;
     gate-held  -> Bring it back.
   All mutating buttons carry data-submit (the v10 pointer-pair gate) and destructive items
   confirm in place. Drafts survive a refresh (helm.drafts, keyed by thread).              */
"use strict";

(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (t, cls, text) => { const e = document.createElement(t); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";

  // The middle pane's host (index.html #view-conv), filled once.
  const view = $("#view-conv");
  view.innerHTML = `
    <div class="conv-panel">
      <header class="conv-head">
        <div class="conv-head-main">
          <div class="conv-crumb hm-crumb"></div>
          <h2 class="conv-title"></h2>
          <div class="conv-byline"></div>
          <div class="conv-sub"></div>
        </div>
        <button class="conv-props-toggle" type="button" title="What this thread is connected to"><span class="conv-props-ico"></span>Connected</button>
      </header>
      <div class="conv-banner hidden"></div>
      <div class="conv-timeline" aria-live="polite"></div>
      <button class="conv-jump hidden" type="button">Jump to latest</button>
      <footer class="conv-foot">
        <div class="conv-ask"></div>
        <div class="conv-confirm hidden" role="alertdialog"></div>
        <div class="conv-sendrow">
          <textarea class="conv-note" rows="1" maxlength="8000" placeholder="Write on this thread"></textarea>
          <label class="attach-btn conv-attach hidden" tabindex="0" title="Attach files — png / jpg / webp / pdf / txt / log, 25 MB each" aria-label="Attach files">
            <span class="attach-ico" aria-hidden="true"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.9-8.9a3.3 3.3 0 0 1 4.7 4.7l-8.9 8.9a1.7 1.7 0 0 1-2.4-2.4l8.2-8.2"/></svg></span>
            <input class="attach-input" type="file" multiple accept="image/png,image/jpeg,image/webp,application/pdf,text/plain,.log" hidden>
          </label>
          <button class="conv-send btn primary" type="button" data-submit>Send</button>
        </div>
        <ul class="attach-staged conv-staged"></ul>
        <div class="conv-foot-acts"></div>
        <div class="conv-hint"></div>
      </footer>
    </div>`;
  const timeline = $(".conv-timeline", view);
  const jumpBtn = $(".conv-jump", view), noteBox = $(".conv-note", view);
  const sendBtn = $(".conv-send", view), askBox = $(".conv-ask", view), footActs = $(".conv-foot-acts", view);
  const foot = $(".conv-foot", view), confirmBox = $(".conv-confirm", view);
  const hint = $(".conv-hint", view), banner = $(".conv-banner", view);
  const attachLabel = $(".conv-attach", view), attachInput = $(".attach-input", view), stagedList = $(".conv-staged", view);
  $(".conv-props-toggle", view).onclick = () => { if (typeof Helm !== "undefined") Helm.go(Helm.propsHref(Helm.route)); };

  let openTid = null, openSec = "threads", openThread = null, data = null, es = null, pollTimer = null;
  let goneTid = null;           // the open thread's id once the site answered 404 for it (nothing to poll)
  let confirming = null;  // {tid, text, run}: a settling tap waiting on its Yes (work, )
  let staged = [];              // files chosen for the next answer (lit cards)
  let sendMode = "note";        // what the Send button does: note | note-only | text-answer | reopen | unbacklog | redirect
  // Every conversation is Helm's own (`console: "none"`): the composer writes through the
  // store's routes, never a console route.
  function settle(d) {
    d.thread = openThread;
    d.local = true;
    if (!d.events || !d.events.length) d.events = openThread ? localTimeline(openThread) : [];
    return d;
  }
  // L11: a thin timeline never looks complete. When the conversation did not answer, the pane is
  // drawn from the thread's own read (localConversation) and the banner says so.
  function sideBanner(d) {
    if (d && d.fallback_note) {
      banner.textContent = "Read from the thread itself: " + d.fallback_note + ". Press R to look again.";
      banner.classList.remove("hidden");
    } else banner.classList.add("hidden");
  }
  const hxName = () => (typeof hx !== "undefined" && hx.appName) ? hx.appName() : ((document.querySelector('meta[name="application-name"]') || {}).content || "Helm");
  const isWorkSite = () => typeof Helm !== "undefined" && Helm.site === "work";
  const KIND_LABEL = { "state-change": "state", "filed": "filed", "update": "producer update", "failure-detail": "failure detail",
    "rule-breach": "rule breach", "log-tail": "log tail", "chat-note": "chat note", "park": "parked", "unpark": "unparked",
    "backlog": "to the backlog", "unbacklog": "pulled back", "note": "note", "instruction": "instruction", "close": "closed" };
  const optWords = (v) => v === "__redirect__" ? "with a note" : v === "__task__" ? "handed it over" : v === "ack" ? "acknowledged"
    : v === "note-only" ? "a note only" : String(v == null ? "" : v).split(/[_-]+/).filter(Boolean).join(" ");
  // Every read is for the thread it was started for, never for whatever is open when the answer lands:
  // `openTid` moves under a slow read (a new open, a removed one), and building the URL from it mid-refresh
  // read a thread the caller never asked about — the missing-thread route spent a 404 on the stranger's
  // behalf (live sweep 2026-11-21; a loaded host widens that window to seconds).
  const threadUrl = (t) => `/api/triage/threads/${encodeURIComponent(t == null ? openTid : t)}`;
  // "Use the draft": a report that carries a checked draft shows it, and the newest unused one gets a
  // button. The press is the reply value below (helm.store.USE_DRAFT_PREFIX), by the owner, after a confirm.
  const USE_DRAFT_PREFIX = "__use_draft__:";
  const USE_DRAFT = /^__use_draft__:/;
  const DRAFT_PLACE = { mail: "Drafts", tickets: "Tickets", wiki: "Wiki", chat: "Chat" };

  const RELAY_AUTHORS = { "relay-mcp": "Relay", "relay": "Relay" };   // keep in step with helm/conversation.py

  // The thread's own timeline (the read API), typed like the feed's.
  function localTimeline(t) {
    const evs = [];
    const events = t.events || [];
    // The live ask is the composer's (nothing drawn twice, v32): the newest bot-ask on a lit card
    // stays out of the timeline; an older ask shows its prompt line, not the options text.
    const lastAskIdx = t.state === "needs-you" && t.ask && t.ask.kind && t.ask.kind !== "none"
      ? events.map(e => e.kind).lastIndexOf("bot-ask") : -1;
    const askLine = (body) => String(body || "").replace(/^ASKED YOU\s*\([^)]*\):\s*/i, "").split("\n")[0].trim();
    for (const [i, e] of events.entries()) {
      if (!e.body && e.kind !== "bot-ask") continue;
      if (i === lastAskIdx) continue;
      // A relay that answered on the owner's word (its source is its token principal) is named, not "You"
      // (helm/conversation.py: the same rule). The owner's own tap, a chat session and a sourceless event stay his.
      const src = String(e.source || "").trim();
      const machine = ["", "owner", "session"].includes(src.toLowerCase()) ? "" : (RELAY_AUTHORS[src] || src);
      if (e.kind === "owner-reply") {
        const opt = ((t.ask || {}).options || []).find(o => o.value === e.body);
        const used = USE_DRAFT.test(e.body || "") ? events.find(x => x.draft && x.draft.sha === e.body.slice(USE_DRAFT_PREFIX.length)) : null;
        const label = opt ? opt.label : optWords(e.body);
        evs.push({ type: "message", author: machine || "You", kind: "reply", ts: e.ts,
                   body: USE_DRAFT.test(e.body || "") ? "You pressed: " + (used ? used.draft.button : "Use the draft")
                                                      : (machine ? machine + " answered: " : "You answered: ") + label });
      } else if (e.kind === "owner-note") evs.push({ type: "message", author: machine || "You", kind: "note", ts: e.ts, body: e.body, delivered: true });
      else if (e.kind === "note" && (e.source || "").toLowerCase() === "owner") evs.push({ type: "message", author: "You", kind: "note", ts: e.ts, body: e.body });
      else if (e.kind === "bot-result") evs.push({ type: "message", author: e.source || "agent", kind: "report", ts: e.ts, body: e.body, ...(e.draft ? { draft: e.draft } : {}) });
      else if (e.kind === "bot-ask") evs.push({ type: "ask", ts: e.ts, body: askLine(e.body), ask: null });
      else evs.push({ type: "system", ts: e.ts, author: e.source || "helm", body: (KIND_LABEL[e.kind] || e.kind) + " · " + e.body });
    }
    return evs;
  }
  // The fallback reads the thread this read was for (see threadUrl): a refresh whose tid moved on must not
  // write the open thread's state either — the paint is guarded, the state has to be.
  async function localConversation(tid) {
    const t = await api(threadUrl(tid));
    if (t && t.thread_id && tid === openTid) openThread = t;
    return { local: true, thread: t, thread_state: t.state, task_id: null, task_status: null, events: localTimeline(t), ask: t.ask,
             properties: {}, review_state: null, pending_notes: [] };
  }

  const nearBottom = () => timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 160;
  const scrollToEnd = () => { timeline.scrollTop = timeline.scrollHeight; };
  // Opening a card with a live ask starts at the ask's words (the last entry of the log), so it reads from its first line.
  // The end of the report above it stays in view; once he scrolls himself, the timeline follows the newest line as before.
  let userScrolled = false;
  for (const ev of ["wheel", "touchmove", "pointerdown", "keydown"]) timeline.addEventListener(ev, () => { userScrolled = true; }, { passive: true });
  const hasStory = () => storyBox.isConnected && storyBox.childNodes.length > 0;
  const scrollToStory = () => {
    if (!hasStory()) return scrollToEnd();
    const tl = timeline.getBoundingClientRect().top, st = timeline.scrollTop;
    const at = (n) => n.getBoundingClientRect().top - tl + st;
    // an entry's top edge meets the top of the log, never half an entry: the report above the ask when it fits
    // in a third of the view, otherwise the ask itself a little way down
    const prev = storyBox.previousElementSibling;
    let top = at(storyBox) - 12;
    if (prev && at(storyBox) - at(prev) <= timeline.clientHeight * 0.34) top = at(prev) - 6;
    timeline.scrollTop = Math.max(0, top);
  };
  const follow = () => { if (!userScrolled && hasStory()) scrollToStory(); else scrollToEnd(); };

  // ---------- event rendering ----------
  // The log grammar (helm.css .hm-log): a time gutter, a node on the rule, the body. Every entry keeps its own class
  // (.conv-msg, .conv-sys, .conv-ask-ev) so the tests and the composer find it as before.
  const clockOf = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  const dayOf = (ts) => {
    if (!ts) return "";
    const d = new Date(ts * 1000), n = new Date(), y = new Date(); y.setDate(n.getDate() - 1);
    if (d.toDateString() === n.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  };
  function logEntry(cls, ts, glyph, tone) {
    const n = el("div", cls + " hm-log-entry" + (tone ? " " + tone : ""));
    const t = el("div", "hm-log-t", clockOf(ts)); if (ts) { const sm = el("small", null, dayOf(ts)); t.append(sm); t.title = fmtTs(ts); }
    const nd = el("div", "hm-log-nd"); const i = el("i"); if (glyph && typeof Helm !== "undefined" && Helm.glyph) i.append(Helm.glyph(glyph, 13)); nd.append(i);
    const bd = el("div", "hm-log-bd");
    n.append(bd, t, nd);   // the words first in the DOM (they read first; the grid puts the time in the gutter and the node on the rule)
    return [n, bd];
  }
  function evNode(e) {
    if (e.type === "ask") return askEventNode(e);
    if (e.type === "system") {
      const [n, bd] = logEntry("conv-sys", e.ts, null, "quiet");
      const body = el("span", "conv-sys-text");
      body.append(typeof renderLinkedText === "function" ? renderLinkedText(e.body || "") : document.createTextNode(e.body || ""));
      bd.append(body);
      n.title = (e.author || "system");
      return n;
    }
    // message
    const mine = (e.author || "").toLowerCase() === "you";
    const [n, bd] = logEntry("conv-msg" + (mine ? " mine" : ""), e.ts, mine ? "pen" : e.kind === "attachment" ? "note" : "thread", mine ? "t-ask" : "t-thread");
    bd.append(el("div", "conv-msg-who", mine ? "You" : e.author || "agent"));
    const body = el("div", "conv-msg-body");
    body.append(typeof renderBody === "function" ? renderBody(e.body || "") : document.createTextNode(e.body || ""));
    bd.append(body);
    if (e.kind === "attachment" && e.att_id && window.Helm && Helm.previewAttachment) {
      // A file an agent (or the owner) put on the card: open it here, in place.
      const row = el("div", "conv-msg-files");
      const open = el("button", "pv-open", "Open"); open.type = "button"; open.title = "Open this file here";
      const tid = openTid, att = e.att_id;
      open.onclick = () => Helm.previewAttachment(tid, att);
      row.append(open); bd.append(row);
    }
    if (e.draft && e.kind === "report") bd.append(draftNode(e.draft, e.ts));
    if (e.delivered && mine && e.kind === "note") {
      bd.append(el("div", "conv-msg-state delivered", "delivered"));
    }
    return n;
  }

  // The draft a report carried: its labeled fields, the exact text, the plan as the approval shows it,
  // a link, and (for the newest unused draft only) the button. Older or used drafts fold shut.
  function draftNode(d, ts) {
    const u = thread().use_draft;
    // Only the newest report that carries a draft can hold the live button: two reports with the same
    // draft share a sha, so the sha alone would light both.
    const withDraft = (thread().events || []).filter(x => x.kind === "bot-result" && x.draft);
    const newest = withDraft.length ? withDraft[withDraft.length - 1].ts === ts : true;
    const live = !!(newest && u && u.sha === d.sha);
    const v = d.view || {};
    const box = el("div", "conv-draft" + (live ? " live" : ""));
    const body = el("div", "conv-draft-body");
    body.append(el("div", "conv-draft-title", v.title || d.button));
    if ((v.fields || []).length) {
      const dl = el("dl", "conv-draft-fields");
      for (const f of v.fields) dl.append(el("dt", null, f.label), el("dd", null, f.value));
      body.append(dl);
    }
    body.append(el("pre", "conv-draft-text", v.text || ""));
    if (v.detail) {
      const det = el("details", "conv-draft-detail");
      det.append(el("summary", null, "The plan as the approval shows it"), el("pre", "conv-draft-plan", v.detail));
      body.append(det);
    }
    if (v.url) {
      const a = el("a", "conv-draft-link", "Open the destination"); a.href = v.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      body.append(a);
    }
    if (live) {
      const b = el("button", "conv-draft-use", d.button);
      b.type = "button"; b.setAttribute("data-submit", ""); b.dataset.sha = d.sha;
      b.title = "Writes exactly this text to " + (DRAFT_PLACE[d.dest] || d.dest) + ". You confirm first.";
      b._act = () => askUseDraft(d);
      body.append(b);
    }
    if (live) {
      box.append(el("div", "conv-draft-head", `Draft ready · ${DRAFT_PLACE[d.dest] || d.dest}`), body);
      return box;
    }
    const det = el("details", "conv-draft-old");
    const key = openTid + "|draft:" + d.sha.slice(0, 12);
    det.open = typeof isFolded === "function" ? !isFolded(key, true) : false;
    det.addEventListener("toggle", () => { if (typeof setFold === "function") setFold(key, !det.open); });
    det.append(el("summary", "conv-draft-head", `Draft · ${DRAFT_PLACE[d.dest] || d.dest} · used or replaced`), body);
    box.append(det);
    return box;
  }
  // The confirm after the press names the button, where it goes and who is emailed (or the chat's members).
  function askUseDraft(d) {
    const v = d.view || {};
    const who = (v.fields || []).find(f => /^(emailed|members?)\b/i.test(f.label)) || (v.fields || []).find(f => /^(to|recipients?)\b/i.test(f.label));
    const text = `${d.button}? This writes to ${DRAFT_PLACE[d.dest] || d.dest}.` + (who ? ` ${who.label}: ${who.value}.` : "")
      + " It is written once, exactly as shown, and nothing is sent until you say Yes.";
    const run = async () => {
      const r = await tap("reply", { value: USE_DRAFT_PREFIX + d.sha, note: "" }, `Sent to be written: ${d.button}`);
      if (!r) await refresh();   // refused (replaced or already used): redraw so the card shows what is true
    };
    confirming = { tid: openTid, text, run }; renderConfirm();
  }

  // A historical ask renders as a log entry; the LIVE ask (needs-you) is the "Your call" block under the log.
  function askEventNode(e) {
    const [n, bd] = logEntry("conv-ask-ev", e.ts, "ask", "t-ask");
    bd.append(el("div", "conv-ask-ev-head", "Asked you"));
    bd.append(el("div", "conv-ask-ev-prompt", e.body || ""));
    const ctx = e.ask && e.ask.context;
    if (ctx && String(ctx).trim()) {
      const c = el("div", "conv-ask-ctx");
      c.append(el("div", "conv-ask-ctx-label", "Context"));
      c.append(el("div", "conv-ask-ctx-body", String(ctx).trim()));
      bd.append(c);
    }
    return n;
  }

  // ---------- timeline ----------
  function renderTimeline() {
    const wasNear = nearBottom();
    timeline.replaceChildren();
    const evs = (data && data.events) || [];
    if (!evs.length) {
      const em = el("div", "conv-empty");
      em.append(el("b", "conv-empty-t", "Nothing said yet"), el("span", null, "The agent's report and your notes will appear here."));
      timeline.append(em);
    }
    for (const e of evs) timeline.append(evNode(e));
    if (storyBox.childNodes.length) timeline.append(storyBox);
    if (wasNear) follow();
    updateJump();
  }

  function updateJump() {
    // the pill stays away while the ask's own box is on screen: the opening scroll stops there on purpose
    let inView = false;
    if (hasStory()) { const r = storyBox.getBoundingClientRect(), t = timeline.getBoundingClientRect(); inView = r.top < t.bottom - 40 && r.bottom > t.top; }
    jumpBtn.classList.toggle("hidden", nearBottom() || inView);
  }
  timeline.addEventListener("scroll", updateJump);
  jumpBtn.onclick = () => { scrollToEnd(); };

  // ---------- the composer ----------
  const thread = () => (data && data.thread) || openThread || {};
  const litAsk = () => { const t = thread(); const st = data ? data.thread_state : t.state; return st === "needs-you" && t.ask && t.ask.kind && t.ask.kind !== "none" ? t.ask : null; };
  const gateWord = () => (typeof gateOf === "function" ? gateOf(thread()) : "");
  const gateHeld = () => { const t = thread(); return t.state === "waiting-external" && String(t.gate_cmd || "").startsWith("helm-gate:build "); };
  const gateSettled = () => thread().state === "done" && gateWord().startsWith("settled by gate");

  function renderStaged() {
    stagedList.replaceChildren(...staged.map((f, i) => {
      const li = el("li", "staged-item");
      li.append(el("span", "staged-name", `${f.name} (${Math.max(1, Math.round(f.size / 1024))} KB)`));
      const x = el("button", "staged-x", "remove"); x.type = "button";
      x.onclick = () => { staged.splice(i, 1); renderStaged(); paintSend(); };
      li.append(x);
      return li;
    }));
  }
  attachInput.onchange = () => {
    for (const f of attachInput.files || []) {
      if (f.size > 25 * 1024 * 1024) { toast(`${f.name} is over 25 MB`, true); continue; }
      if (staged.some(s => s.name === f.name && s.size === f.size)) continue;
      staged.push(f);
    }
    attachInput.value = "";
    renderStaged(); paintSend();
  };
  attachLabel.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); attachInput.click(); } });
  const readStaged = () => Promise.all(staged.map(f => new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onerror = () => rej(new Error(`could not read ${f.name}`));
    fr.onload = () => res({ filename: f.name, mime: f.type || "", content_base64: String(fr.result).split(",")[1] || "" });
    fr.readAsDataURL(f);
  })));
  const withFiles = async (body) => staged.length ? { ...body, attachments: await readStaged() } : body;
  const noteVal = () => noteBox.value.trim();
  function paintSend() {
    // "Send note only" needs a note or a file; the other modes need words.
    sendBtn.disabled = sendMode === "note-only" ? !(noteVal() || staged.length) : false;
    // beside the options the note is the second choice: quiet; everywhere else Send is the primary action
    sendBtn.classList.toggle("primary", sendMode !== "note-only");
    sendBtn.classList.toggle("secondary", sendMode === "note-only");
  }
  noteBox.addEventListener("input", () => { paintSend(); noteBox.style.height = "auto"; noteBox.style.height = Math.min(160, noteBox.scrollHeight) + "px"; });

  // A tap on the thread through the store (helm-web's act(): one lock, the draft cleared on
  // acceptance, the lists re-read). The conversation re-reads itself after.
  // While it is out the composer is dimmed and inert : the foot
  // outlives the redraw, so the busy state is cleared here, after the re-read or on a refusal.
  async function tap(verb, body, okMsg, transform) {
    if (confirming) { confirming = null; renderConfirm(); }
    const mine = !foot.classList.contains("busy");
    const hadFocus = document.activeElement === noteBox;
    if (mine) { foot.classList.add("busy"); foot.inert = true; }
    try {
      const r = await act(null, { thread_id: openTid }, verb, body, okMsg, transform);
      if (r) { staged = []; renderStaged(); noteBox.value = ""; noteBox.style.height = ""; await refresh(); }
      return r;
    } finally {
      if (mine) {
        foot.classList.remove("busy"); foot.inert = false;
        if (hadFocus) noteBox.focus({ preventScroll: true });
      }
    }
  }

  // Settling asks first: an earlier build confirmed a terminal option,
  // Close and Mute, so one stray tap cannot take a card off the list. The tap draws a strip
  // naming the card and what settles it; Yes (submit-gated) does it, No sends nothing. The
  // pending ask lives here, not in the strip, so a live redraw of the composer keeps it.
  function settleFirst(text, run) {
    if (!isWorkSite()) return run;
    return () => { confirming = { tid: openTid, text: text(), run }; renderConfirm(); };
  }
  function renderConfirm() {
    const c = confirming && confirming.tid === openTid ? confirming : null;
    confirmBox.classList.toggle("hidden", !c);
    // Drawn once per ask: a redraw between a press and its release would drop the Yes (the v10 gate).
    if (confirmBox._for === c) return;
    confirmBox._for = c;
    if (!c) { confirmBox.replaceChildren(); return; }
    const yes = el("button", "btn primary conv-confirm-yes", "Yes");
    markSubmit(yes, () => { confirming = null; renderConfirm(); c.run(); });
    const no = el("button", "btn quiet conv-confirm-no", "No");
    no.type = "button";
    no.onclick = () => { confirming = null; renderConfirm(); };
    confirmBox.replaceChildren(el("span", "conv-confirm-text", c.text), yes, no);
  }
  const cardTitle = () => { const t = thread(); return t.title && t.title !== t.thread_id ? t.title : "this card"; };

  // The live ask's words are the newest entry of the log: an ask node, the time it was put to him, then the story.
  const storyBox = el("div", "conv-storybox");
  function storyEntry(story, ts) {
    const [n, bd] = logEntry("conv-ask-now", ts, "ask", "t-ask");
    bd.append(el("div", "conv-ask-ev-head", "Asked you"), story);
    return n;
  }
  // An option's name; "(Recommended)" at its end becomes the brass mark (the words stay in the text).
  function optLabel(text) {
    const lbl = el("span", "lbl");
    const m = /^(.*?)\s*(\(Recommended\))\s*$/i.exec(String(text));
    if (!m) { lbl.textContent = String(text); return lbl; }
    lbl.append(m[1] + " ");
    const rec = el("span", "conv-rec", m[2]); lbl.append(rec);
    return lbl;
  }
  const unfoldedAbout = new Set();   // card + summary text of the About lines he has opened
  function renderComposer() {
    askBox.replaceChildren(); footActs.replaceChildren(); storyBox.replaceChildren();
    const t = thread();
    const st = data ? data.thread_state : t.state;
    const ask = litAsk();
    const work = isWorkSite();
    attachLabel.classList.add("hidden");
    sendMode = "note";
    sendBtn.textContent = "Send";
    noteBox.placeholder = "Write on this thread";
    if (ask) {
      // ---- a lit card: the ask, answered here (L1: without scrolling at 999×1392) ----
      const wrap = el("div", "conv-live-ask conv-call");   // the decision, docked under the log
      const story = el("div", "conv-live-ask conv-story");   // the ask's words: they are the last entry of the log
      // How old this question is and what has happened since, from the store's times (catchupChips in app.js).
      const chips = catchupChips(t);
      if (chips.length) {
        const cu = el("div", "conv-catchup");
        for (const c of chips) { const x = el("span", "conv-cu-chip" + (c.cls ? " " + c.cls : ""), c.text); if (c.title) x.title = c.title; cu.append(x); }
        story.append(cu);
      }
      // The producer's whole summary, "Needs you because:" leading :
      // a long one folds behind "more" so the options stay in view (L1), never cut.
      const summ = (typeof cleanSummary === "function" ? cleanSummary(t.summary) : (t.summary || "")).trim();
      // The producer's cold-read of the question (ask.recap, kept by the store only while it is still the live
      // question) replaces the boilerplate summary: it says what this is and why it needs him.
      const recap = ((t.catchup && t.catchup.recap) || "").trim();
      if (recap) {
        const rc = el("div", "conv-recap");
        rc.append(el("div", "conv-recap-label", "Where this stands"), el("div", "conv-recap-body", recap));
        story.append(rc);
      }
      if (!recap && summ && summ.toLowerCase() !== (ask.prompt || "").trim().toLowerCase()) {
        const about = el("div", "conv-about folded");
        const more = el("button", "about-more hidden", "more"); more.type = "button";
        about.append(el("span", "about-label", "About: "), el("span", "about-desc", summ), more);
        // The composer is drawn again on every refresh: a summary he opened stays open (kept by card and text).
        const foldKey = (t.thread_id || "") + "\n" + summ;
        more.onclick = () => {
          const folded = about.classList.toggle("folded"); more.textContent = folded ? "more" : "less";
          if (folded) unfoldedAbout.delete(foldKey); else unfoldedAbout.add(foldKey);
        };
        if (unfoldedAbout.has(foldKey)) { about.classList.remove("folded"); more.classList.remove("hidden"); more.textContent = "less"; }
        else requestAnimationFrame(() => {
          const d = about.querySelector(".about-desc");
          if (d.scrollHeight > d.clientHeight + 1) more.classList.remove("hidden"); else about.classList.remove("folded");
        });
        story.append(about);
      }
      story.append(el("div", "conv-live-prompt", ask.prompt || ""));
      // The Human-First context paragraph (SC7), under the prompt and above the options; then how-to.
      if (ask.context && String(ask.context).trim()) {
        const b = el("div", "conv-ask-ctx-body"); b.append(renderBody(String(ask.context).trim()));
        if (recap) {
          // With a recap on the first screen the context is detail: folded, and remembered per card.
          const d = el("details", "conv-ask-ctx fold");
          const key = openTid + "|ask-context";
          d.open = !(typeof isFolded === "function" && isFolded(key, true));
          d.addEventListener("toggle", () => { if (typeof setFold === "function") setFold(key, !d.open); });
          d.append(el("summary", "conv-ask-ctx-label", "Details"), b);
          story.append(d);
        } else {
          const ctx = el("div", "conv-ask-ctx");
          ctx.append(el("div", "conv-ask-ctx-label", "Context"));
          ctx.append(b);
          story.append(ctx);
        }
      }
      if (ask.how_to && String(ask.how_to).trim()) {
        const how = el("div", "conv-ask-ctx howto");
        how.append(el("div", "conv-ask-ctx-label", "How to"));
        const b = el("div", "conv-ask-ctx-body"); b.append(renderBody(String(ask.how_to).trim())); how.append(b);
        story.append(how);
      }
      let list = [];
      if (ask.kind === "choice" && (ask.options || []).length) list = ask.options;
      else if ((ask.options || []).length > 1) list = ask.options;
      else if (ask.kind === "ack") { const o = (ask.options || [])[0]; list = [{ label: o?.label || "Got it", value: o?.value || "ack", effect: o?.effect || "" }]; }
      const reply = (body, msg) => tap("reply", body, msg, withFiles);
      if (ask.kind === "text") {
        noteBox.placeholder = "Your answer"; sendMode = "text-answer"; sendBtn.textContent = "Send answer";
        wrap.append(el("h3", "conv-yourcall-t", "Your answer"));
      } else {
        const call = el("div", "conv-yourcall");
        call.append(el("h3", "conv-yourcall-t", list.length > 1 ? "Your call" : "Your answer"),
                    el("p", "conv-yourcall-s hm-meta", list.length > 1 ? "Pick one. Your note, if any, goes with it." : "Your note, if any, goes with it."));
        wrap.append(call);
        const opts = el("div", "conv-opts"); opts.setAttribute("role", "group"); opts.setAttribute("aria-label", "Your answer");
        for (const o of list) {
          // An option that settles the card (the store's `terminal`, or an ack's "ack") says so and is
          // drawn quieter: ending it must not look like starting work.
          const settles = work && (o.terminal || (ask.kind === "ack" && o.value === "ack"));
          const cls = settles ? "opt-terminal" : typeof classifyOption === "function" ? classifyOption(o) : "";
          const b = el("button", "conv-opt" + (cls ? " " + cls : ""));
          b.append(optLabel(o.label || o.value));
          if (settles) b.append(el("span", "conv-opt-tag", "settles it"));
          if (o.effect) b.append(el("span", "eff", o.effect));
          b.type = "button"; b.setAttribute("data-submit", ""); b.title = o.effect || "";
          const answer = () => {
            if (o.value === "amend" && !noteVal()) { noteBox.focus(); noteBox.placeholder = "Describe what you want amended or changed…"; toast("Type your requested changes in the note, then tap Amend to send", false); return; }
            reply({ value: o.value, note: noteVal() }, `Replied: ${o.label}`);
          };
          // A terminal option settles the card on the tap (the store's `terminal`): it asks first.
          b._act = o.terminal ? settleFirst(() => `Settle “${cardTitle()}” with “${o.label || o.value}”? It comes off the list; a reopen asks fresh.`, answer) : answer;
          opts.append(b);
        }
        wrap.append(opts);
        noteBox.placeholder = "Add a note. It is sent with your answer.";
        sendMode = "note-only"; sendBtn.textContent = "Send note only"; sendBtn.title = "Send the note without picking an option (Ctrl+Enter)";
      }
      if (wrap.childNodes.length) askBox.append(wrap);
      storyBox.replaceChildren(storyEntry(story, askedAt(t)));
      attachLabel.classList.remove("hidden");
      // the quiet line of secondary actions helm-web's card carried (v32)
      if (ask.kind !== "text" && (ask.kind === "choice" || list.length > 1)) {
        const mute = el("button", "btn quiet", "Don't ask again"); mute.title = "Mute this kind of card. You won't be asked about it again.";
        markSubmit(mute, settleFirst(() => `Mute “${cardTitle()}”? It closes, nothing like it is asked again, and a muted card does not come back to reopen.`,
                                     () => tap("mute", { note: noteVal() }, "Muted — won't ask about this kind again")));
        footActs.append(mute);
      }
        if (work) {
        const bl = el("button", "btn quiet", "Backlog"); bl.title = "Not now. Needs a line in the note saying why.";
        markSubmit(bl, () => { if (!noteVal()) { noteBox.focus(); return toast("Type a line in the note first: what this is and why it is not now", true); } tap("backlog", { reason: noteVal() }, "In the backlog — pull it back from Threads"); });
        footActs.append(bl);
        footActs.append(quietClose());
      }
      footActs.append(snoozeRow());
      hint.textContent = ask.kind === "text" ? "Your words are the answer. A file can ride with them." : "Tapping an option answers the card, with your note if you wrote one. Send note only keeps it open with your words on it.";
    } else if (st === "needs-you") {
      // a lit card with no ask: close is the one action (plus snooze)
      const close = el("button", "conv-opt", ""); close.append(el("span", "lbl", "Close"), el("span", "eff", "I take it off the list"));
      close.type = "button"; close.setAttribute("data-submit", "");
      close._act = closeTap();
      const wrap = el("div", "conv-live-ask"); const opts = el("div", "conv-opts"); opts.append(close); wrap.append(opts); askBox.append(wrap);
      footActs.append(snoozeRow());
      sendMode = "redirect"; hint.textContent = "Nothing is asked; Close takes it off the list. A note here answers in your words.";
    } else if (gateHeld() || gateSettled()) {
      const w = el("div", "conv-live-ask gate-kept");
      w.append(el("div", "conv-live-prompt", (gateHeld() ? "Held by gate: " : "Settled by gate: ") + gateWord().replace(/^(held|settled) by gate \([^)]*\): /, "")));
      const back = el("button", "conv-opt", ""); back.append(el("span", "lbl", "Bring it back"), el("span", "eff", "Relights the question the gate answered for you"));
      back.type = "button"; back.setAttribute("data-submit", "");
      back._act = () => tap("reopen", { note: "brought back from the gate" }, "Brought back — it's in Needs you");
      const opts = el("div", "conv-opts"); opts.append(back); w.append(opts); askBox.append(w);
      sendMode = "reopen"; hint.textContent = "The gate kept this from you. Bring it back to answer it yourself.";
    } else {
      // A note is an answer in the owner's words
      // ("none of these, do what my note says"); on a settled card it reopens with the note.
      if (st === "done") { sendMode = "reopen"; hint.textContent = "A note here reopens the card; it asks fresh rather than guessing back the old options."; noteBox.placeholder = "Reopen this with a note…"; sendBtn.textContent = "Reopen"; }
      else if (st === "backlog") { sendMode = "unbacklog"; hint.textContent = "In the backlog. A note here pulls it back to Needs you with your words."; noteBox.placeholder = "Pull it back with a note…"; sendBtn.textContent = "Pull back"; }
      else if (st === "working") { sendMode = "redirect"; hint.textContent = "Answered; something is acting on it. A note here answers again to change course."; noteBox.placeholder = "Change course with a note…"; }
      else if (st === "waiting-external") { sendMode = "redirect"; hint.textContent = "Waiting on someone else. A note here is an answer in your words and puts it back in flight."; }
      else { sendMode = "redirect"; hint.textContent = "A note here answers the card with your words: none of these, do what my note says."; noteBox.placeholder = "What you want done…"; }
      if (work && st === "backlog") {
        const b = el("button", "btn gate-back", "Pull it back — this is now"); b.title = "Back to Needs you with the question it kept";
        markSubmit(b, () => tap("unbacklog", {}, "Pulled back — it needs you again")); footActs.append(b);
      }
    }
    // The affordances on a wait: park / unpark, submit-gated.
    if (work && st === "waiting-external" && !(t.wake_at && t.wake_at * 1000 > Date.now())) {
      if (t.parked) {
        const b = el("button", "btn quiet", "Unpark — this is moving again");
        markSubmit(b, () => tap("unpark", {}, "Unparked — back in flight")); footActs.append(b);
      } else {
        const b = el("button", "btn quiet", "Park this"); b.title = "Nothing is actually pushing it; say why in the note (a sentence or two, 80+ characters)";
        markSubmit(b, () => { const r = noteVal(); if (r.length < 80) { noteBox.focus(); return toast("Say why in a sentence or two (80+ characters) — a one-line reason is as unreviewable as none", true); } tap("park", { reason: r }, "Parked"); });
        footActs.append(b);
      }
    }
    // Most cards settle by closing them: a quiet close sits on every
    // open card (). A lit card got its own above; a settled one reopens instead.
    if (work && st !== "needs-you" && st !== "done") footActs.append(quietClose());
    if (confirming && st === "done") confirming = null;   // settled elsewhere meanwhile: nothing left to confirm
    renderConfirm();
    paintSend();
  }
  const closeTap = () => settleFirst(() => `Close “${cardTitle()}”? It comes off the list; a reopen asks fresh.`,
                                     () => { const r = noteVal(); tap("close", r ? { reason: r } : {}, "Closed"); });
  function quietClose() {
    const b = el("button", "btn quiet", "Close"); b.title = "Take it off the list. A note, if you wrote one, is the reason.";
    markSubmit(b, closeTap());
    return b;
  }
  function snoozeRow() {
    const row = el("div", "snooze");
    row.setAttribute("role", "group"); row.setAttribute("aria-label", "Snooze");
    row.append(el("span", "snooze-l", "Snooze"));
    const btns = el("div", "snooze-btns");
    for (const [label, hours, msg] of SNOOZE_OPTIONS) {
      const b = el("button", "btn snooze-btn", label);
      b.title = b.ariaLabel = "Snooze for " + ({ "1h": "1 hour", "1d": "1 day", "1w": "1 week", "1mo": "1 month" }[label] || label);
      markSubmit(b, () => { if (!state.offline) tap("snooze", { hours }, msg); });
      btns.append(b);
    }
    row.append(btns);
    return row;
  }

  // ---------- properties (U8): the right pane ----------
  // A deep link can draw the properties before the shell learns the notebook's folders; a
  // link to a note by its path becomes a link once they arrive (shell.js "helm:vaults").
  window.addEventListener("helm:vaults", () => { if (openTid) renderProperties(); });
  // Anything the thread names inside the app is a link to it (the owner 2026-09-26: "if it's a
  // reference to something within the app, it should link to it"). The notebook resolves the
  // thread's source ids and link urls to notes and threads (GET /api/thread/{id}/refs, once
  // per open); the properties draw again when the answer lands.
  // A payload with no thread_state has no thread behind its id (a Helm thread always has a state):
  // the stub openThread only looks like one, so nothing of the thread's is asked for it.
  const noCard = (d) => !!(d && d.thread_state == null);
  const refsCache = new Map();   // thread_id -> {value: {note: rel} | {thread: id}}, null while asked
  function refsFor(tid) {
    if (!tid || typeof Helm === "undefined" || !Helm.has("notebook") || noCard(data)) return {};
    if (refsCache.has(tid)) return refsCache.get(tid) || {};
    refsCache.set(tid, null);
    api(`/api/thread/${encodeURIComponent(tid)}/refs`)
      .then(r => { refsCache.set(tid, (r && r.refs) || {}); if (openTid === tid) renderProperties(); })
      .catch(() => refsCache.set(tid, {}));
    return {};
  }
  const ymdOf = (ts) => { const d = new Date(ts * 1000); return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); };
  const refLink = (ref, text) => {
    if (!ref) return null;
    // A note or a thread the properties point at is an object chip (web/objects.js): its glyph, its colour, a peek.
    const mk = (spec) => { const a = typeof Helm.chip === "function" ? Helm.chip(spec) : Object.assign(el("a", null, spec.label), { href: spec.href }); a.classList.add("conv-prop-link-a"); return a; };
    if (ref.note) return mk({ type: /(^|\/)\d{4}-\d{2}-\d{2}\.md$/.test(ref.note) ? "day" : "note", id: ref.note, label: text, href: Helm.noteHref(ref.note) });
    if (ref.thread) { const th = typeof threadById === "function" ? threadById(ref.thread) : null; return mk({ type: th && th.state === "needs-you" ? "ask" : "thread", id: ref.thread, label: text, href: Helm.convHref(ref.thread) }); }
    return null;
  };
  function renderProperties() {
    // The right pane is the Connected pane (web/objects.js): a compact facts block, then what the thread points at
    // (Links, Attachments) and its own id. Each row keeps .conv-prop-row / .conv-prop-label / .conv-prop-val.
    const sections = [];
    if (!data) { Helm.connected.render(sections); return; }
    const pill = (cls, text) => el("span", "conv-prop-pill " + cls, text);
    const t = thread();
    const facts = { kind: "facts", rows: [] };
    const fact = (label, v) => { if (v != null && v !== "") facts.rows.push([label, v]); };
    const group = (title, node, id, type) => { if (node) sections.push({ title, node, id, type }); };
    // Nothing drawn twice (v32): one Links group, an attachment once, the thread id once.
    const linksWrap = el("div", "conv-prop-links");
    let linkCount = 0;
    // ---- the Helm thread's own fields ----
    if (t && t.thread_id) {
      fact("Status", pill("status-" + (t.state || ""), t.state || "—"));
      fact("Urgency", t.urgency);
      fact("Category", t.category);
      fact("Responder", t.responder);
      if (t.parked) fact("Parked", t.parked_reason || "yes");
      if (t.blocked_on) fact("Waiting on", t.blocked_on);
      if (t.recheck_at) fact("Looks again", fmtTs(t.recheck_at));
      if (t.wake_at && t.wake_at * 1000 > Date.now()) fact("Wakes", fmtTs(t.wake_at));
      if (t.first_seen) fact("Filed", fmtTs(t.first_seen));
      if (t.last_change) fact("Last change", fmtTs(t.last_change));
      const refs = refsFor(t.thread_id);
      const links = (t.links || []).filter(l => l && (l.url || l.label));
      if (links.length) {
        const wrap = linksWrap;
        for (const l of links) {
          const item = el("div", "conv-prop-link-item");
          const url = String(l.url || ""), label = String(l.label || url);
          // The notebook's answer spells the note as it is held; until it lands, the path rule.
          const ref = refs[url.trim()];
          const rel = (ref && ref.note) || (typeof noteRelFromUrl === "function" ? noteRelFromUrl(url) : null);
          if (rel && typeof Helm !== "undefined" && Helm.has("notebook")) item.append(refLink({ note: rel }, label));
          else if (ref && ref.thread) item.append(refLink(ref, label));
          else if (/^https?:\/\//i.test(url)) { const a = el("a", "conv-prop-link-a", label); a.href = url; a.target = "_blank"; a.rel = "noopener"; item.append(a); }
          else item.append(el("span", "conv-prop-link-val", label !== url ? label + ": " + url : url));
          wrap.append(item); linkCount++;
        }
      }
      // Where the thread came from; a source naming a note (a call's minutes) or another thread links to it.
      const srcs = (t.sources || []).filter(s => s != null && s !== "");
      if (srcs.length) {
        const from = el("div", "conv-prop-from");
        srcs.forEach((s, i) => {
          const o = typeof s === "object" ? s : { id: String(s) };
          const text = `${o.source || ""} ${o.id || o.note || ""}`.trim();
          if (i) from.append(", ");
          const linked = refLink(o.id != null ? refs[String(o.id).trim()] : null, text);
          if (linked) { from.append(linked); return; }
          from.append(text);
        });
        fact("From", from);
      }
      sections.push(facts);
      if (linkCount) group("Links", linksWrap, "links", "note");
      // The day it was filed, as a day chip (the notebook keeps a page for every day).
      if (t.first_seen && Helm.has("notebook") && typeof Helm.chip === "function" && typeof Helm.dayHref === "function") {
        const ymd = ymdOf(t.first_seen), box = el("div", "conv-days");
        box.append(Helm.chip({ type: "day", id: ymd, label: ymd, href: Helm.dayHref(ymd) }), el("p", "hm-meta", "The day this was filed: " + clockOf(t.first_seen) + "."));
        group("Days", box, "days", "day");
      }
      // Attachments the thread holds (the store's own), thumbnails with the lightbox.
      const atts = t.attachments || [];
      if (atts.length) {
        const grid = el("div", "visuals-grid conv-visuals");
        for (const a of atts) {
          const url = a.url || `/api/triage/threads/${encodeURIComponent(t.thread_id)}/attachments/${a.att_id}`;
          const isImg = (a.mime || "").startsWith("image/") || /\.(png|jpe?g|webp|gif)$/i.test(a.filename || "");
          if (isImg) {
            const card = el("div", "visual-card");
            const img = document.createElement("img"); img.src = url; img.alt = a.caption || a.filename || "visual"; img.className = "visual-img"; img.loading = "lazy";
            img.onclick = () => openLightbox(url, a.caption || a.filename);
            card.append(img, el("div", "visual-caption", a.filename || ""));
            grid.append(card);
          } else {
            const kind = window.Helm && Helm.attachmentKind ? Helm.attachmentKind(a.mime, a.filename) : "none";
            if (kind === "html" || kind === "diff" || kind === "text") {
              // What an agent built opens here, in place: a page in a sandboxed frame, a diff as coloured lines.
              const view = el("button", "sent-file pv-open", `${Helm.attachmentLabel[kind]} · ${a.filename || a.att_id}`);
              view.type = "button"; view.onclick = () => Helm.previewAttachment(t.thread_id, a);
              grid.append(view);
            }
            const link = el("a", "sent-file"); link.href = url; link.target = "_blank"; link.rel = "noopener";
            link.append(el("span", "sent-name", `${kind === "html" || kind === "diff" || kind === "text" ? "⬇" : "📄"} ${a.filename || a.att_id}`));
            grid.append(link);
          }
        }
        group("Attachments", grid, "attachments", "note");
      }
      // The thread's own link: open it in a tab, or copy it to paste somewhere.
      const self = el("a", "conv-prop-link-a conv-prop-self"); self.href = Helm.convHref(t.thread_id, openSec); self.title = "This thread's own link";
      self.append(el("code", null, t.thread_id));
      group("Thread", self, "thread", "thread");
    } else if (linkCount) group("Links", linksWrap, "links", "note");
    const things = sections.reduce((n, g) => n + (g.id === "links" ? linkCount : g.id === "days" ? 1 : g.id === "attachments" ? ((t.attachments || []).length) : 0), 0);
    Helm.connected.render(sections, { lede: things ? "Everything this thread points at, and what is working on it." : "Nothing else is connected to this thread yet.", count: things });
  }

  // ---------- sending ----------
  async function sendNote() {
    if (!openTid) return;
    const text = noteVal();
    if (sendMode === "text-answer") {
      if (!text && !staged.length) return toast("Type an answer first", true);
      return tap("reply", { value: text || "(see attached)" }, "Sent", withFiles);
    }
    if (sendMode === "note-only") {
      if (!text && !staged.length) return toast("Write something first", true);
      return tap("reply", { value: NOTE_ONLY, note: text }, staged.length ? "Sent with file(s)" : "Note sent", withFiles);
    }
    if (!text) return toast("Write something first", true);
    if (sendMode === "reopen") return tap("reopen", { note: text }, "Reopened — it asks again in Needs you");
    if (sendMode === "unbacklog") return tap("unbacklog", { note: text }, "Pulled back — it needs you again");
    return tap("reply", { value: "__redirect__", note: text }, "Sent your instruction");
  }
  sendBtn._act = sendNote;   // gated (data-submit): the pointer gate and Enter are its only wiring
  noteBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!sendBtn.disabled) sendNote(); return; }
    if (e.key === "Enter" && !e.shiftKey && sendMode !== "note-only") { e.preventDefault(); sendNote(); }
  });

  // ---------- data + push ----------
  // A live run pane (H.hooks.convRunPane), between the banner and the timeline; the element exists only while a hook has drawn something.
  function runPane(tid) {
    const nodes = typeof Helm !== "undefined" && Helm.hooks ? Helm.hooks.convRunPane.run({ tid, thread: openThread, data }) : [];
    let box = $(".conv-runpane", view);
    if (!nodes.length) { if (box) box.remove(); return; }
    if (!box) { box = el("div", "conv-runpane"); banner.after(box); }
    box.replaceChildren(...nodes);
    fitMessages();
  }
  // While a run pane is shown, at every width: when it leaves less room than one whole message, the timeline would show a
  // cut-off sliver of a card. Then it is hidden behind one row that says so; the row's button puts the run pane away so the
  // messages show. Without a run pane the conversation is as it always was.
  const MIN_MSG_ROOM = 130;
  const squeeze = el("div", "conv-squeeze"); squeeze.hidden = true;
  const squeezeText = el("span", "conv-squeeze-text", "Messages are hidden while the run pane is open.");
  const squeezeBtn = el("button", "conv-squeeze-btn", "Show messages"); squeezeBtn.type = "button"; squeezeBtn.title = "Put the run pane away so the messages show";
  squeeze.append(squeezeText, squeezeBtn);
  timeline.after(squeeze);   // after, not before: the run pane stays directly above the timeline
  let runsAway = false;
  squeezeBtn.addEventListener("click", () => { runsAway = !runsAway; fitMessages(); if (runsAway) scrollToEnd(); });
  function fitMessages() {
    const was = timeline.hidden;
    fitMessagesNow();
    if (was && !timeline.hidden) scrollToEnd();   // a timeline that comes back opens at the newest message
  }
  function fitMessagesNow() {
    const panel = $(".conv-panel", view), box = $(".conv-runpane", view);
    if (panel) panel.style.setProperty("--foot-h", foot.offsetHeight + "px");   // the Jump to latest pill rides above the composer
    if (!panel || !box || !box.childNodes.length) {
      if (box) box.hidden = false;
      squeeze.hidden = true; timeline.hidden = false; runsAway = false; return;
    }
    box.hidden = runsAway;
    if (runsAway) {
      squeeze.hidden = false; timeline.hidden = false; squeezeText.textContent = "The run pane is put away.";
      squeezeBtn.textContent = "Show run pane"; squeezeBtn.title = "Bring the run pane back"; return;
    }
    let used = 0;
    for (const c of panel.children) if (c !== timeline && c !== squeeze && !c.hidden && getComputedStyle(c).position !== "absolute") used += c.offsetHeight;
    const room = panel.clientHeight - used;
    const tight = room < MIN_MSG_ROOM;
    squeeze.hidden = !tight; timeline.hidden = tight;
    squeezeText.textContent = "Messages are hidden while the run pane is open."; squeezeBtn.textContent = "Show messages";
    squeezeBtn.title = "Put the run pane away so the messages show";
  }
  // A pane that draws its own box on its own clock changes the panel's children: watch them and their sizes.
  {
    const panel = $(".conv-panel", view);
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => fitMessages()) : null;
    const watchKids = () => { if (ro) for (const c of panel.children) if (c !== timeline && c !== squeeze) ro.observe(c); };
    if (ro) ro.observe(panel);
    watchKids();
    new MutationObserver(() => { watchKids(); fitMessages(); }).observe(panel, { childList: true });
  }
  async function refresh() {
    if (!openTid) return;
    const tid = openTid;
    try {
      let d;
      try {
        const conv = await api(`/api/conversations/${encodeURIComponent(tid)}`);
        const t = noCard(conv) ? null : await api(threadUrl(tid)).catch(() => null);
        if (t && t.thread_id && tid === openTid) openThread = t;
        d = conv;
      } catch (e) {
        if (tid !== openTid) return;   // the open moved on under this answer: the read is for nobody
        // 404: the site does not hold the thread, and asking the read API would only say it again.
        if (/^404\b/.test(e.message)) { threadGone(tid); return; }
        d = await localConversation(tid);
        d.fallback_note = `the conversation did not answer (${e.message})`;
      }
      if (tid !== openTid) return;
      if (goneTid === tid) { goneTid = null; startPush(); }   // it is back: poll and push again
      settle(d);
      data = d;
      if (openThread && !openThread.attachments) {
        api(threadUrl(tid) + "/attachments").then(a => { if (openThread && tid === openTid) { openThread.attachments = (a && a.attachments) || []; renderProperties(); } }).catch(() => {});
      }
      paintHead();
      renderComposer();
      renderTimeline();
      renderProperties();
      sideBanner(d);
      runPane(tid);
    } catch (e) {
      if (tid !== openTid) return;
      if (/^404\b/.test(e.message)) { threadGone(tid); return; }
      hint.textContent = `conversation unreachable: ${e.message}`;
      banner.textContent = `The conversation could not be read: ${e.message}`;
      banner.classList.remove("hidden");
    }
  }
  // A thread the site does not hold (deleted, or a stale link in a tab left open) is not going to
  // appear because the page asked again: say so once and stop the 30 s poll and the push. R, or
  // any action that re-reads, still asks, and a thread that answers again resumes both.
  function threadGone(tid) {
    if (tid !== openTid) return;
    goneTid = tid;
    stopPush();
    const msg = "This thread is no longer in " + hxName() + " (it may have been removed). Press R to look again.";
    timeline.replaceChildren(el("div", "conv-empty", msg));
    banner.textContent = msg;
    banner.classList.remove("hidden");
    hint.textContent = "";
  }
  const SEC_NAME = { "needs-you": "Needs you", threads: "Threads", activity: "Activity", search: "Search" };
  const URG_WORD = { now: "Now", "this-week": "This week", whenever: "Whenever" };
  function paintHead() {
    const realTitle = openThread && openThread.title && openThread.title !== openThread.thread_id ? openThread.title : "";
    $(".conv-title", view).textContent = realTitle || (data && data.properties && data.properties.title) || (data && data.task_id) || openTid;
    const bits = [];
    if (data && data.thread_state) bits.push(data.thread_state);
    if (data && data.task_status && data.task_status !== data.thread_state) bits.push(`task: ${data.task_status}`);
    if (data && data.task_id) bits.push(data.task_id);
    $(".conv-sub", view).textContent = bits.join(" · ");
    // the crumb: the section he came from, then the thread's label
    const t = openThread || {}, crumb = $(".conv-crumb", view);
    crumb.replaceChildren();
    const sec = document.createElement("a"); sec.textContent = SEC_NAME[openSec] || "Threads"; sec.href = "#/" + (SEC_NAME[openSec] && openSec !== "search" ? openSec : "threads");
    crumb.append(sec);
    if (t.category) crumb.append(el("span", "sl", "/"), el("span", null, t.category));
    // the byline: how urgent, who asked and when
    const by = $(".conv-byline", view); by.replaceChildren();
    const st = data ? data.thread_state : t.state;
    if (st === "needs-you" && t.urgency) by.append(el("span", "hm-pill conv-urg urg-" + t.urgency + (t.urgency === "now" ? " now" : ""), URG_WORD[t.urgency] || t.urgency));
    // phone: the sub line is hidden, so the state rides in the byline as a pill
    if (bits.length) { const sp = el("span", "hm-pill conv-state-pill", data && data.task_status && data.task_status !== data.thread_state ? `${data.thread_state || ""} · task ${data.task_status}`.replace(/^ · /, "") : bits[0]); sp.title = bits.join(" · "); by.append(sp); }
    const asked = askedAt(t);
    const lastAsk = ((t.events || []).filter(e => e.kind === "bot-ask").slice(-1)[0]) || null;
    const who = String((lastAsk && lastAsk.source) || t.responder || "").split(":")[0];
    if (st === "needs-you" && asked) by.append(el("span", null, `Asked${who && who !== "owner" ? " by " + who : ""} at ${clockOf(asked)}, ${fmtAge(asked)}`));
    else if (t.last_change) by.append(el("span", null, `Last change ${fmtAge(t.last_change)}`));
    const src = (t.sources || [])[0];
    if (src && typeof src === "object" && (src.source || src.id) && !/^t-/.test(String(src.id || ""))) by.append(el("span", null, `from ${[src.source, src.id].filter(Boolean).join(" ")}`));
  }

  function startPush() {
    stopPush();
    try {
      es = new EventSource(`/api/conversations/${encodeURIComponent(openTid)}/stream`);
      es.onmessage = (m) => {
        try {
          const d = JSON.parse(m.data);
          const changed = !data || JSON.stringify(d.events) !== JSON.stringify(data.events)
            || JSON.stringify(d.pending_notes) !== JSON.stringify(data.pending_notes)
            || JSON.stringify(d.properties) !== JSON.stringify(data.properties)
            || d.review_state !== data.review_state;
          const stateChanged = !data || d.thread_state !== data.thread_state;
          data = settle(d);
          if (changed || stateChanged) { paintHead(); renderComposer(); renderTimeline(); renderProperties(); sideBanner(d); }
        } catch { /* partial frame */ }
      };
      es.onerror = () => { /* EventSource retries; keep a slow poll as a backstop */ };
    } catch { /* no EventSource: polling below carries it */ }
    startPoll();
  }
  function startPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => { if (!document.hidden) refresh(); }, 30_000);
  }
  function stopPush() {
    if (es) { try { es.close(); } catch {} es = null; }
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // R (and the ↻ button) re-reads the open conversation too.
  if (typeof Helm !== "undefined") Helm.on("refresh", () => { if (openTid) refresh(); });

  // ---------- open / close (the shell's route #/<section>/<id> drives both) ----------
  window.openConversation = (tid, sec) => {
    if (!tid) return;
    if (openTid === tid) { renderProperties(); return; }
    openTid = tid; userScrolled = false; runsAway = false; openSec = sec || "threads"; data = null; goneTid = null; staged = []; renderStaged();
    confirming = null; renderConfirm();
    refsCache.delete(tid);   // what it names is asked afresh each time it opens
    openThread = (typeof threadById === "function" && threadById(tid)) || { thread_id: tid, title: tid };
    noteBox.dataset.draft = tid + ":note";
    noteBox.value = typeof drafts !== "undefined" && typeof drafts[tid + ":note"] === "string" ? drafts[tid + ":note"] : "";
    noteBox.style.height = "";
    timeline.replaceChildren(el("div", "conv-empty", "reading…"));
    askBox.replaceChildren(); footActs.replaceChildren(); hint.textContent = "";
    paintHead();
    Helm.setProps("Properties", null);
    document.querySelectorAll(".lrow.on").forEach(r => r.classList.remove("on"));
    document.querySelectorAll(`.lrow[data-id="${CSS.escape(tid)}"]`).forEach(r => r.classList.add("on"));
    refresh().then(follow);
    startPush();
  };
  function closeConversation() {
    stopPush();
    openTid = null; openThread = null; data = null; goneTid = null;
    const rp = $(".conv-runpane", view); if (rp) rp.remove();
    runsAway = false; fitMessages();
    document.querySelectorAll(".lrow.on").forEach(r => r.classList.remove("on"));
    Helm.setProps("Properties", null);
  }
  window.closeConversation = closeConversation;
  window.conversationOpen = () => openTid;

  // The 60 s poll: when the open thread moved (answered, closed, woke), the composer follows.
  if (typeof Helm !== "undefined") Helm.on("threads", () => {
    if (!openTid) return;
    const t = typeof threadById === "function" ? threadById(openTid) : null;
    if (t && openThread && (t.state !== openThread.state || t.last_change !== openThread.last_change)) refresh();
  });

  // Anything carrying data-conv opens the conversation by route (a feed row…).
  document.addEventListener("click", (e) => {
    const b = e.target.closest ? e.target.closest("[data-conv]") : null;
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    const tid = b.getAttribute("data-conv");
    Helm.go(Helm.convHref(tid, b.getAttribute("data-conv-sec") || undefined));
  }, true);
})();
