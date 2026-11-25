/* The page's own errors, reported (2026-09-26 live sweep). Until now a JavaScript error on
   the live app reached nobody: it showed in a console no one had open. Every uncaught error
   and unhandled rejection is POSTed once to /api/client-error; the web role logs it and hands
   it to the error tracker, whose sweep cards each NEW issue to an agent. Loaded first, in <head>, so
   the boot of every other script is covered.
   Not reported: network failures (offline, the hourly sign-in lapse: not a bug in the page),
   aborted fetches, the browser's own "ResizeObserver loop" notice, errors from extensions.
   At most 10 reports per page load, each distinct message once.                             */
(() => {
  const MAX = 10, sent = new Set();
  const NOISE = /ResizeObserver loop|^(TypeError: )?(Failed to fetch|NetworkError when attempting to fetch resource\.?|Load failed|network error)$|AbortError|The (operation|user) (was|aborted)/i;
  function report(kind, msg, src, line, col, stack) {
    try {
      msg = String(msg || "").slice(0, 300);
      if (!msg || NOISE.test(msg)) return;
      if (src && !/^https?:/.test(src)) return;                   // chrome-extension://, moz-extension://
      const key = kind + "|" + msg + "|" + src + "|" + line;
      if (sent.has(key) || sent.size >= MAX) return;
      sent.add(key);
      const body = JSON.stringify({ kind, msg, src: src || "", line: line || 0, col: col || 0,
        stack: String(stack || "").slice(0, 4000), view: location.hash.slice(0, 120), ua: navigator.userAgent.slice(0, 200) });
      fetch("/api/client-error", { method: "POST", credentials: "same-origin", keepalive: true, redirect: "manual",
        headers: { "Content-Type": "application/json" }, body }).catch(() => {});
    } catch { /* the reporter must never be the next error */ }
  }
  window.addEventListener("error", (e) => {
    if (!(e instanceof ErrorEvent)) return;                      // a failed <img>/<script> load, not a script error
    const err = e.error;
    report("error", (err && err.name && err.message) ? `${err.name}: ${err.message}` : e.message, e.filename, e.lineno, e.colno, err && err.stack);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    const msg = r instanceof Error ? `${r.name}: ${r.message}` : (typeof r === "string" ? r : (() => { try { return JSON.stringify(r); } catch { return String(r); } })());
    const at = r && r.stack ? /\((https?:[^)]+):(\d+):(\d+)\)|@(https?:[^\s]+):(\d+):(\d+)/.exec(r.stack) : null;
    report("rejection", msg, at ? (at[1] || at[4]) : "", at ? +(at[2] || at[5]) : 0, at ? +(at[3] || at[6]) : 0, r && r.stack);
  });
})();
