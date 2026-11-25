/* Helm modules — the section registry (U0b).

   Every section of the left rail is registered here, or by the file that owns it, with
   `H.register({...})` (H is the shell, `Helm`; this file loads before shell.js and keeps the
   list, shell.js builds its SECTIONS from it). Nothing in shell.js, index.html or sw.js has to
   change when a section is added or a later unit fills one in.

     key      the hash route and the data-sec of the rail row:  #/<key>[/<part>/<part>...]
     label    the rail row's text
     module   the /api/meta module that switches it on (H.has(module)); a section whose module
              is off is not in the rail and its route falls back to NEEDS YOU
     order    where it sits in the rail (lower first); defaults to 100
     list     true: the section holds a list in the left pane (host #list-<key>)
     listParts  how many route parts still show the list on a phone (CHAT: 1, the profile; the session makes it a page)
     page     the middle pane's host, #view-<page> (a section that is only a list has none)
     title    the row's tooltip, and the line the `?` key sheet shows
     noProps  true: the right pane is not drawn unless something puts properties in it (H.setProps)
     route    (parts, route) called on every navigation into the section; `parts` are the
              decoded hash parts after the key (#/chat/p/s -> ["p","s"])
     mount    ({list, view}, route) called once when the section is entered from another
     unmount  () called when another section is entered
     count    () the number the row shows (or null); the section may also call H.setCount

   H.hooks: three places a later unit adds to a page it does not own. Each is
   `H.hooks.<name>.add(fn)`; the owning page calls `H.hooks.<name>.run(...)` and uses what comes
   back (a Node, an array of Nodes, or nothing). A hook nobody added to is a no-op.
     needsYouExtra   run({ host, route })        nodes drawn above the Helm cards in NEEDS YOU
     activityBands   run({ view, route })        nodes added to ACTIVITY's page
     convRunPane     run({ tid, thread, data })  one node: a live run pane in a conversation

   H.features: where the files that are not sections put themselves, `H.features[name] = api`,
   so another file can find them without a load order.   */
"use strict";
(() => {
  const items = [];
  const reg = {
    items,
    register(def) {
      if (!def || !def.key) throw new Error("a section needs a key");
      const at = items.findIndex((s) => s.key === def.key);
      const s = Object.assign({ module: "inbox", order: 100, list: false, title: def.label || def.key }, def);
      if (s.plainOrder == null) s.plainOrder = s.order;
      if (at >= 0) items[at] = s; else items.push(s);
      if (reg.onChange) reg.onChange(s);
      return s;
    },
    get: (key) => items.find((s) => s.key === key),
    // The registered sections, in rail order.
    sorted: () => items.slice().sort((a, b) => a.order - b.order),
  };
  const hook = () => {
    const fns = [];
    return {
      add(fn) { if (typeof fn === "function" && !fns.includes(fn)) fns.push(fn); },
      remove(fn) { const i = fns.indexOf(fn); if (i >= 0) fns.splice(i, 1); },
      get count() { return fns.length; },
      // Every hook's nodes, flattened; one that throws is logged and skipped (a page never breaks for an extra).
      run(...args) {
        const out = [];
        for (const fn of fns.slice()) {
          try {
            const r = fn(...args);
            if (r == null) continue;
            for (const n of Array.isArray(r) ? r : [r]) if (n) out.push(n);
          } catch (e) { console.error("hook", e); }
        }
        return out;
      },
    };
  };
  reg.hooks = { needsYouExtra: hook(), activityBands: hook(), convRunPane: hook() };
  reg.features = {};
  window.HelmRegistry = reg;

  // The sections. Rail order: Today, Needs you, Threads, Notebook, Activity.
  reg.register({ key: "home", label: "Today", module: "inbox", order: 5, page: "home", noProps: true, title: "Today: what is waiting, what is moving, what came in" });
  reg.register({ key: "needs-you", label: "Needs you", module: "inbox", order: 10, list: true, title: "The cards waiting on you: answers first" });
  reg.register({ key: "threads", label: "Threads", module: "inbox", order: 20, list: true, title: "Every thread this deployment has ever held, done ones included" });
  reg.register({ key: "notebook", label: "Notebook", module: "notebook", order: 30, page: "notebook", title: "Day, notes, topics (Ctrl+D = today)" });
  reg.register({ key: "activity", label: "Activity", module: "activity", order: 40, page: "activity", list: true, title: "Heartbeat, today's counts, every event" });
})();
