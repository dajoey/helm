/* Open what an agent built from the card it reported on: a page, a diff, a screenshot, a text file.

   Helm.previewAttachment(threadId, attachment | attachmentId) opens a full-screen viewer:
     html   a frame onto /api/triage/threads/<id>/attachments/<att>/view. The frame is sandboxed
            (scripts only: no same-origin, popups, top navigation, forms or downloads) and the
            server sends a policy that allows no request of any kind, so the page runs with no
            way to reach Helm, its cookies, its storage or any server. What it can still do is
            navigate its own frame (no header stops that): it could send what is typed into it
            to another address, so the bar says not to type anything into it. "Open full
            screen" opens the same address in a tab, where a host page frames it under the
            same warning.
     diff   the file as coloured lines (text only, never HTML).
     text   the file as plain text.
     image  the existing lightbox.
   Helm.attachmentKind(mime, filename) is the one rule for which of these a file is. It mirrors
   helm/preview.py preview_kind: the stored type decides when it names a kind, the file name only
   when the type says nothing, so a ".html" name over text/plain is text and is never framed. */
(() => {
  "use strict";
  const H = window.Helm || (window.Helm = {});
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const appName = () => (document.querySelector('meta[name="application-name"]') || {}).content || "Helm";

  const DIFF_TYPES = ["text/x-diff", "text/x-patch", "text/diff", "application/x-patch", "application/x-diff"];
  const TEXT_TYPES = ["text/plain", "text/markdown", "text/csv", "text/x-log", "application/json"];
  const EXT_KIND = { ".html": "html", ".htm": "html", ".diff": "diff", ".patch": "diff", ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image", ".txt": "text", ".md": "text", ".log": "text", ".csv": "text", ".json": "text" };
  function kindOf(mime, filename) {
    const base = String(mime || "").split(";")[0].trim().toLowerCase();
    const m = /\.[^./\\]*$/.exec(String(filename || ""));
    const ext = m ? m[0].toLowerCase() : "";
    if (base === "text/html") return "html";
    if (DIFF_TYPES.includes(base)) return "diff";
    if (base.startsWith("image/")) return "image";
    if (TEXT_TYPES.includes(base)) return base === "text/plain" && (ext === ".diff" || ext === ".patch") ? "diff" : "text";
    if (base === "" || base === "application/octet-stream") return EXT_KIND[ext] || "none";
    return "none";
  }
  H.attachmentKind = kindOf;
  H.attachmentLabel = { html: "Preview page", diff: "View changes", text: "View text", image: "View image" };

  const MAX_DIFF_LINES = 5000, MAX_TEXT_CHARS = 400000;
  let ov = null, frameBox = null, bodyEl = null, nameEl = null, noteEl = null, fullA = null, dlA = null, fitBtn = null, closeBtn = null;
  let returnFocus = null, skipPop = false, fit = false, token = 0;

  const base = (tid) => "/api/triage/threads/" + encodeURIComponent(tid) + "/attachments";
  const attUrl = (tid, id) => base(tid) + "/" + encodeURIComponent(id);

  function build() {
    if (ov) return;
    ov = el("div", "pv hidden"); ov.id = "preview-ov"; ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true"); ov.setAttribute("aria-label", "File preview");
    const bar = el("div", "pv-bar");
    nameEl = el("span", "pv-name");
    noteEl = el("span", "pv-note");
    fitBtn = el("button", "pv-btn", "Fit width"); fitBtn.type = "button"; fitBtn.title = "Show the page at desktop width, scaled to this screen";
    fitBtn.onclick = () => { fit = !fit; applyFit(); };
    fullA = el("a", "pv-btn", "Open full screen"); fullA.target = "_blank"; fullA.rel = "noopener noreferrer";
    dlA = el("a", "pv-btn", "Download"); dlA.rel = "noopener";
    closeBtn = el("button", "pv-close", "✕"); closeBtn.type = "button"; closeBtn.title = "Close (Esc)"; closeBtn.setAttribute("aria-label", "Close");
    closeBtn.onclick = () => close(false);
    bar.append(nameEl, noteEl, fitBtn, fullA, dlA, closeBtn);
    bodyEl = el("div", "pv-body");
    ov.append(bar, bodyEl);
    document.body.append(ov);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !ov.classList.contains("hidden")) { e.preventDefault(); e.stopPropagation(); close(false); }
    }, true);
    window.addEventListener("popstate", () => {
      if (skipPop) { skipPop = false; return; }
      if (!ov.classList.contains("hidden")) close(true);
    });
    window.addEventListener("resize", () => { if (frameBox && fit) applyFit(); });
  }

  // The frame's stage: at actual size the frame fills the window; "Fit width" lays the page out
  // 980 px wide and scales it down to the window, the way a phone shows a desktop page.
  function applyFit() {
    if (!frameBox) return;
    const f = frameBox.firstChild;
    fitBtn.textContent = fit ? "Actual size" : "Fit width";
    if (!fit) { frameBox.style.cssText = ""; f.style.cssText = ""; return; }
    const W = 980, s = Math.min(1, frameBox.clientWidth / W);
    f.style.width = W + "px"; f.style.height = (frameBox.clientHeight / s) + "px";
    f.style.transform = "scale(" + s + ")"; f.style.transformOrigin = "0 0";
  }

  function show(name, note, kind) {
    build();
    nameEl.textContent = name || "file";
    noteEl.textContent = note || "";
    ov.dataset.kind = kind;
    bodyEl.replaceChildren();
    frameBox = null; fit = false;
    fitBtn.classList.toggle("hidden", kind !== "html");
    fullA.classList.toggle("hidden", kind !== "html");
    if (ov.classList.contains("hidden")) {
      returnFocus = document.activeElement;
      ov.classList.remove("hidden");
      document.body.classList.add("pv-open");
      try { history.pushState({ helmPreview: true }, "", location.href); } catch (e) { /* no history: Esc and the button still close it */ }
    }
    closeBtn.focus();
  }

  function close(fromPop) {
    if (!ov || ov.classList.contains("hidden")) return;
    token++;
    ov.classList.add("hidden");
    document.body.classList.remove("pv-open");
    bodyEl.replaceChildren();   // the frame goes with it: a page that was running stops
    frameBox = null;
    if (!fromPop && history.state && history.state.helmPreview) { skipPop = true; history.back(); }
    if (returnFocus && returnFocus.focus) { try { returnFocus.focus(); } catch (e) { /* gone */ } }
    returnFocus = null;
  }
  H.closePreview = () => close(false);

  function renderDiff(text) {
    const wrap = el("div", "pv-diff");
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    let add = 0, del = 0, files = 0;
    const out = document.createDocumentFragment();
    lines.slice(0, MAX_DIFF_LINES).forEach((ln) => {
      let cls = "pv-ctx";
      if (/^diff --git |^\+\+\+ |^--- |^index |^new file mode|^deleted file mode|^similarity index|^rename (from|to) /.test(ln)) {
        cls = "pv-file"; if (ln.startsWith("diff --git ")) { files++; cls = "pv-file pv-fhead"; }
      } else if (ln.startsWith("@@")) cls = "pv-hunk";
      else if (ln.startsWith("+")) { cls = "pv-add"; add++; }
      else if (ln.startsWith("-")) { cls = "pv-del"; del++; }
      else if (ln.startsWith("\\")) cls = "pv-meta";
      out.append(el("div", "pv-line " + cls, ln === "" ? " " : ln));
    });
    const head = el("div", "pv-diff-head", (files ? files + (files === 1 ? " file, " : " files, ") : "") + "+" + add + " −" + del);
    wrap.append(head, out);
    if (lines.length > MAX_DIFF_LINES) wrap.append(el("div", "pv-more", (lines.length - MAX_DIFF_LINES) + " more lines are in the download."));
    return wrap;
  }

  async function open(tid, att) {
    build();
    let meta = att;
    if (typeof att === "string") {
      const first = ++token;
      show(att, "Reading the file", "text");
      try {
        const r = await fetch(base(tid), { cache: "no-store", credentials: "same-origin" });
        const j = await r.json();
        meta = ((j && j.attachments) || []).find((a) => a.att_id === att);
      } catch (e) { meta = null; }
      if (first !== token) return;   // closed (or another file opened) while the list was loading
      if (!meta) { bodyEl.replaceChildren(el("div", "pv-msg", "That file is not on this thread any more.")); noteEl.textContent = ""; return; }
    }
    const kind = kindOf(meta.mime, meta.filename);
    const url = attUrl(tid, meta.att_id);
    const name = meta.filename || meta.att_id;
    if (kind === "image" && typeof openLightbox === "function") {
      if (ov && !ov.classList.contains("hidden")) close(false);
      openLightbox(url, meta.caption || name);
      return;
    }
    const my = ++token;
    show(name, kind === "html" ? "A run's page in a sandbox: it cannot see " + appName() + " or your sign-in. Do not type anything into it." : (meta.mime || ""), kind);
    dlA.href = url; dlA.setAttribute("download", name);
    if (kind === "html") {
      fullA.href = url + "/view";   // a tab gets the host page that frames this page, never the page itself
      const f = document.createElement("iframe");
      f.className = "pv-frame";
      f.setAttribute("sandbox", "allow-scripts");   // never allow-same-origin, -popups, -top-navigation, -forms
      f.setAttribute("referrerpolicy", "no-referrer");
      f.setAttribute("title", name);
      f.src = url + "/view?raw=1";
      frameBox = el("div", "pv-stage");
      frameBox.append(f);
      bodyEl.replaceChildren(frameBox);
      return;
    }
    if (kind === "diff" || kind === "text") {
      bodyEl.replaceChildren(el("div", "pv-msg", "Reading the file"));
      try {
        const r = await fetch(url, { cache: "no-store", credentials: "same-origin" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const text = await r.text();
        if (my !== token) return;
        if (kind === "diff") bodyEl.replaceChildren(renderDiff(text));
        else {
          const pre = el("pre", "pv-text", text.length > MAX_TEXT_CHARS ? text.slice(0, MAX_TEXT_CHARS) : text);
          bodyEl.replaceChildren(pre);
          if (text.length > MAX_TEXT_CHARS) bodyEl.append(el("div", "pv-more", "The rest is in the download."));
        }
      } catch (e) {
        if (my === token) bodyEl.replaceChildren(el("div", "pv-msg", "Could not open " + name + ": " + e.message));
      }
      return;
    }
    bodyEl.replaceChildren(el("div", "pv-msg", "There is no viewer for this kind of file. Use Download."));
  }
  H.previewAttachment = (tid, att) => open(tid, att);
})();
