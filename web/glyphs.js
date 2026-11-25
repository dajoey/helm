/* Helm object glyphs: one inline-SVG set, no network. Loaded right after shell.js.
   Helm.glyph(type[, size])   -> an <svg class="hm-gl"> element (stroke = currentColor, viewBox 0 0 24 24)
   Helm.glyphHTML(type[, size]) -> the same as an HTML string (for template-built markup)
   Helm.glyphBody(type)         -> just the inner path markup (a caller that draws its own <svg>)
   Helm.objectTypes           -> { type: { label, cls } } for the nine object types; cls is the .t-<type> colour class
   Object types: ask note day chat task thread run job host. Also: private, plus the navigation and action set
   (home needs book more threads activity search plus pen check). An unknown type draws the
   note page so nothing renders empty. Colours come from helm.css (.t-<type> sets --c; .hm-gl tints with it). */
"use strict";
(() => {
  const H = window.Helm;
  if (!H) return;
  const PATHS = {
    ask: '<path d="M12 3l8 9-8 9-8-9z"/><path d="M12 9v4M12 16v.01"/>',
    note: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
    day: '<path d="M3 18h18M7 18a5 5 0 0110 0M12 5v3M5 9l2 2M19 9l-2 2"/>',
    chat: '<path d="M4 5h16v11H10l-5 4v-4H4z"/>',
    task: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>',
    thread: '<path d="M4 6h16v12H4z"/><path d="M4 7l8 6 8-6"/>',
    run: '<circle cx="12" cy="12" r="8.5"/><path d="M10 8.5l5 3.5-5 3.5z"/>',
    job: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
    host: '<path d="M12 21s6-5.5 6-10a6 6 0 10-12 0c0 4.5 6 10 6 10z"/><circle cx="12" cy="11" r="2"/>',
    private: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 018 0v2.5"/>',
    home: '<circle cx="12" cy="12" r="8.5"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    needs: '<path d="M5 21V4M5 5h13l-3 4 3 4H5"/>',
    book: '<path d="M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3z"/><path d="M5 17a3 3 0 013-3h11M9 8h6"/>',
    more: '<circle cx="6" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18" cy="12" r="1.3"/>',
    threads: '<path d="M4 7h16M4 12h16M4 17h10"/>',
    activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    pen: '<path d="M4 20l1-4L16 5l3 3L8 19z"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  };
  const TYPES = {
    ask: "Ask", note: "Note", day: "Day", chat: "Chat", task: "Board task", thread: "Thread", run: "Run", job: "Scheduled job", host: "Host",
  };
  const objectTypes = {};
  for (const k of Object.keys(TYPES)) objectTypes[k] = { label: TYPES[k], cls: "t-" + k };
  const SVGNS = "http://www.w3.org/2000/svg";
  const body = (type) => PATHS[type] || PATHS.note;
  const glyphHTML = (type, size) => {
    const s = size ? ' style="width:' + (+size || 16) + "px;height:" + (+size || 16) + 'px"' : "";
    return '<svg class="hm-gl" viewBox="0 0 24 24" aria-hidden="true" focusable="false"' + s + ">" + body(type) + "</svg>";
  };
  const glyph = (type, size) => {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "hm-gl");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    if (size) { svg.style.width = (+size || 16) + "px"; svg.style.height = (+size || 16) + "px"; }
    svg.innerHTML = body(type);
    return svg;
  };
  H.glyph = glyph;
  H.glyphHTML = glyphHTML;
  H.glyphBody = body;   // the inner SVG markup alone (the rail draws its own <svg>)
  H.glyphTypes = Object.keys(PATHS);
  H.objectTypes = objectTypes;
})();
