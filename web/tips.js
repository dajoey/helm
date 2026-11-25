/* Helm — hover tips (2026-09-17). Anything carrying a title or aria-label gets an immediate, themed
   tooltip on mouse hover or keyboard focus, so icon-only controls say what they are. The native
   title is parked in data-tip while the pointer is over the element (no double tooltip) and put
   back afterwards. Touch is left alone. */
(() => {
  "use strict";
  const tip = document.createElement("div"); tip.className = "tip"; tip.setAttribute("role", "tooltip"); tip.hidden = true;
  let cur = null, timer = null;
  const pick = (n) => n && n.closest ? n.closest("[title], [data-tip], button[aria-label], a[aria-label]") : null;
  function hide() {
    clearTimeout(timer); tip.hidden = true;
    if (cur && cur.dataset.tip && !cur.dataset.tipKeep) { cur.title = cur.dataset.tip; delete cur.dataset.tip; }
    cur = null;
  }
  function show(el) {
    if (el === cur) return; hide(); cur = el;
    if (el.title) { el.dataset.tip = el.title; el.removeAttribute("title"); } else if (el.dataset.tip) el.dataset.tipKeep = "1";
    const text = el.dataset.tip || el.getAttribute("aria-label"); if (!text) { cur = null; return; }
    timer = setTimeout(() => {
      if (!el.isConnected || !el.getClientRects().length) return hide();
      if (!(el.matches(":hover") || el.matches(":focus-visible"))) return hide();   // the tip lives only while the pointer is on it or it holds keyboard focus
      tip.textContent = text; tip.hidden = false; if (!tip.isConnected) document.body.append(tip);
      const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect(), pad = 8;
      let top = r.bottom + 8; if (top + t.height > innerHeight - pad) top = r.top - t.height - 8;
      tip.style.left = Math.round(Math.max(pad, Math.min(innerWidth - t.width - pad, r.left + r.width / 2 - t.width / 2))) + "px";
      tip.style.top = Math.round(Math.max(pad, top)) + "px";
    }, 120);
  }
  // A tip needs a person: a pointer that has never moved sits at 0,0 and the browser still reports it hovering whatever is there (a headless
  // browser, a freshly opened window), and a focus the page gives by itself at load is not a keyboard user arriving. So a pointer event at
  // exactly 0,0 shows nothing, and focus shows a tip only after a key was pressed (and not since a pointer press).
  let keys = false;
  document.addEventListener("keydown", (e) => { if (!/^(Shift|Control|Alt|Meta|CapsLock)$/.test(e.key)) keys = true; }, true);
  document.addEventListener("pointerdown", () => { keys = false; }, true);
  document.addEventListener("pointerover", (e) => {
    if (e.pointerType === "touch") return;
    if (e.clientX === 0 && e.clientY === 0) { hide(); return; }
    const el = pick(e.target); if (el) show(el); else hide();
  });
  // The pointer moving inside the element it was parked on (never having left it) is the first real sign of a person there.
  document.addEventListener("pointermove", (e) => {
    if (e.pointerType === "touch" || (e.clientX === 0 && e.clientY === 0) || cur) return;
    const el = pick(e.target); if (el) show(el);
  }, { passive: true });
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("click", hide, true);                  // a used control's tip goes with the use
  window.addEventListener("hashchange", hide);                      // so does a change of view
  window.addEventListener("popstate", hide);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" || e.key === "Enter") hide(); }, true);
  window.HelmTips = { hide };                                       // for code that moves focus by itself (the drawer)
  document.addEventListener("focusin", (e) => { const el = pick(e.target); if (el && keys && e.target.matches(":focus-visible")) show(el); });
  document.addEventListener("focusout", hide);
  document.addEventListener("scroll", hide, { passive: true, capture: true });
  document.documentElement.addEventListener("pointerleave", hide);
})();
