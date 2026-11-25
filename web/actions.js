/* In-place actions: what an ACTIVITY row can do without leaving the page. Open
   (the conversation, in the middle pane), answer (the same, where the composer holds the live
   ask), wake, snooze, and retry a run that stalled. They go to /api/triage/threads/<id>/<verb>.
   Every outcome is a toast; a refusal says why, and the lists re-read after anything that went
   through.                                                                                      */
"use strict";
const HelmActions = (() => {
  const A = {};
  const enc = encodeURIComponent;
  async function post(path, body) {
    const r = await api(path, { method: "POST", body: JSON.stringify(body || {}) });
    if (r && r.ok === false) throw new Error(r.error || r.detail || "refused");
    return r;
  }
  A.helm = (tid, verb, body) => post(`/api/triage/threads/${enc(tid)}/${verb}`, body || {});

  // Snooze choices, in hours (the thread's own route).
  A.SNOOZE = [["1 hour", 1], ["Tomorrow", 24], ["Next week", 168]];

  // Run one action from a button: disabled while it runs, a toast either way, then re-read.
  A.run = async (btn, done, fn, ctx) => {
    if (btn) btn.disabled = true;
    try {
      const r = await fn();
      toast(done);
      Helm.emit("acted", Object.assign({ result: r }, ctx || {}));
      Helm.emit("refresh");
      return r;
    } catch (e) {
      toast(`didn't go through: ${e.message}`, true);
      return null;
    } finally { if (btn) btn.disabled = false; }
  };

  // A small popover of choices under a button (snooze times).
  let pop = null;
  function closePop() { if (pop) { pop.remove(); pop = null; document.removeEventListener("click", outside, true); } }
  function outside(e) { if (pop && !pop.contains(e.target)) closePop(); }
  A.menu = (anchor, items) => {
    closePop();
    pop = document.createElement("div");
    pop.className = "act-pop";
    pop.setAttribute("role", "menu");
    for (const it of items) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "act-pop-item"; b.textContent = it.label; b.setAttribute("role", "menuitem");
      // A choice here acts (snooze until): the v10 submit gate is its only wiring.
      gate(b, () => { closePop(); it.act(); });
      pop.append(b);
    }
    document.body.append(pop);
    const r = anchor.getBoundingClientRect();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + "px";
    pop.style.top = (r.bottom + 4 + window.scrollY) + "px";
    setTimeout(() => document.addEventListener("click", outside, true), 0);
    const first = pop.querySelector("button"); if (first) first.focus();
  };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });

  // The v10 submit gate (web/app.js): a button that acts fires on a press and release on
  // itself (or Enter), never on a bare click. Its click only has to stay out of the row under it.
  function gate(b, fn) {
    b.setAttribute("data-submit", "");
    b._act = () => {
      if (b.dataset.submitDone === "1") return;
      b.dataset.submitDone = "1";
      setTimeout(() => { delete b.dataset.submitDone; }, 700);
      fn(b);
    };
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); };
  }

  // A row of action buttons. Each item: {label, title, act(btn)} (acts: gated),
  // {label, nav: true, act} (only moves the view) or {label, menu: () => items} (opens choices).
  A.bar = (items) => {
    const bar = document.createElement("div");
    bar.className = "act-bar";
    for (const it of items.filter(Boolean)) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "act-btn" + (it.cls ? " " + it.cls : ""); b.textContent = it.label;
      if (it.title) b.title = it.title;
      b.dataset.act = it.key || it.label.toLowerCase().replace(/[^a-z]+/g, "-");
      if (it.menu || it.nav) {
        b.onclick = async (e) => {
          e.preventDefault(); e.stopPropagation();
          if (it.menu) A.menu(b, await it.menu(b)); else it.act(b);
        };
      } else gate(b, it.act);
      bar.append(b);
    }
    return bar;
  };

  // The runs that went quiet, as ACTIVITY last read them (/api/status quiet_runs).
  A.quiet = new Set();

  // The actions that make sense for a Helm thread, by its state (ACTIVITY rows).
  A.forThread = (t, sec) => {
    if (!t || !t.thread_id) return [];
    const tid = t.thread_id;
    const items = [];
    const snoozeHelm = { key: "snooze", label: "Snooze", title: "Hide it until later; it comes back with the same question", menu: () => A.SNOOZE.map(([label, hours]) => ({
      label, act: () => A.run(null, `Snoozed until ${label.toLowerCase()}`, () => A.helm(tid, "snooze", { hours }), { thread_id: tid }) })) };
    if (t.state === "needs-you") items.push({ key: "answer", nav: true, label: "Answer", title: "Open it; the question is in the composer", act: () => Helm.go(Helm.convHref(tid, "needs-you")) }, snoozeHelm);
    else if (t.state === "working") items.push(
      { key: "open", nav: true, label: "Open", act: () => Helm.go(Helm.convHref(tid, sec || "activity")) },
      // Retry only where a run went quiet (the store's own test, /api/status quiet_runs): on a
      // healthy run it would say "did not report back" and lift the attempt cap for nothing.
      A.quiet.has(tid) && { key: "retry", label: "Retry", title: "The run went quiet or gave up: run it again", act: (b) => A.run(b, "Sent back to run again", () => A.helm(tid, "retry", {}), { thread_id: tid }) });
    else if (t.state === "waiting-external" && t.wake_at && t.wake_at * 1000 > Date.now()) items.push(
      { key: "open", nav: true, label: "Open", act: () => Helm.go(Helm.convHref(tid, sec || "activity")) },
      { key: "wake", label: "Wake now", title: "Bring it back now", act: (b) => A.run(b, "Woken: it asks again in Needs you", () => A.helm(tid, "wake"), { thread_id: tid }) });
    else items.push({ key: "open", nav: true, label: "Open", act: () => Helm.go(Helm.convHref(tid, sec || "activity")) });
    return items.filter(Boolean);
  };
  return A;
})();
