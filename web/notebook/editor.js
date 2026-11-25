// The note editor: CodeMirror 6 from the vendored bundle, with the pieces a
// vault note needs. Wikilinks are styled and open on Ctrl+click, [[ offers
// note names, checkbox lines get a real checkbox, an image paste goes to
// attachments/, the frontmatter folds, and the note saves itself a moment
// after you stop typing (Ctrl+S saves now).

import * as CM from "../vendor/cm.js";

const AUTOSAVE_MS = 1500;
const WIKI_RE = /!?\[\[([^\]\n]+)\]\]/g;
const TASK_RE = /^(\s*[-*+]\s+)\[( |x|X)\](?=\s)/;
const LIST_RE = /^(\s*[-*+]\s+)/;

const theme = CM.EditorView.theme({
  "&": {backgroundColor: "var(--nb-panel)", color: "var(--nb-fg)", fontSize: "14px", borderRadius: "8px"},
  "&.cm-focused": {outline: "none"},
  ".cm-scroller": {fontFamily: "var(--nb-sans)", lineHeight: "1.6"},
  ".cm-content": {padding: "14px 0", caretColor: "var(--nb-fg)"},
  ".cm-line": {padding: "0 18px"},
  ".cm-cursor, .cm-dropCursor": {borderLeftColor: "var(--nb-fg)"},
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
    {backgroundColor: "var(--nb-accent-soft)"},
  ".cm-activeLine": {backgroundColor: "rgba(127,127,127,.06)"},
  ".cm-gutters": {backgroundColor: "var(--nb-panel)", color: "var(--nb-dimmer)", border: "none"},
  ".cm-foldPlaceholder": {backgroundColor: "var(--nb-bg2)", border: "1px solid var(--nb-line)", color: "var(--nb-dimmer)",
                          padding: "0 8px", borderRadius: "4px", fontSize: "12px"},
  ".cm-tooltip": {backgroundColor: "var(--nb-panel2)", border: "1px solid var(--nb-line2)", color: "var(--nb-fg)",
                  borderRadius: "6px", boxShadow: "0 10px 30px -12px rgba(0,0,0,.8)"},
  ".cm-tooltip-autocomplete ul li": {padding: "3px 10px"},
  ".cm-tooltip-autocomplete ul li[aria-selected]": {backgroundColor: "var(--nb-accent-soft)", color: "var(--nb-fg)"},
  ".cm-completionDetail": {color: "var(--nb-dimmer)", fontStyle: "normal", marginLeft: "10px", fontSize: "12px"},
  ".cm-panels": {backgroundColor: "var(--nb-bg2)", color: "var(--nb-fg)", borderColor: "var(--nb-line)"},
  ".cm-panels input, .cm-panels button": {fontFamily: "var(--nb-sans)"},
  ".cm-searchMatch": {backgroundColor: "var(--nb-mark)"},
  ".cm-wikilink": {color: "var(--nb-accent)", cursor: "pointer"},
  ".cm-wikilink.missing": {color: "var(--nb-dimmer)", borderBottom: "1px dotted var(--nb-dimmer)"},
  ".cm-task": {margin: "0 6px 0 0", verticalAlign: "-2px", accentColor: "var(--nb-accent)", cursor: "pointer"},
  ".cm-task-done": {color: "var(--nb-dimmer)", textDecoration: "line-through"},
});

const highlight = CM.HighlightStyle.define([
  {tag: CM.tags.heading1, fontWeight: "700", fontSize: "1.35em", color: "var(--nb-fg)"},
  {tag: CM.tags.heading2, fontWeight: "700", fontSize: "1.18em", color: "var(--nb-fg)"},
  {tag: CM.tags.heading3, fontWeight: "600", fontSize: "1.06em", color: "var(--nb-fg)"},
  {tag: [CM.tags.heading4, CM.tags.heading5, CM.tags.heading6], fontWeight: "600", color: "var(--nb-dim)"},
  {tag: CM.tags.emphasis, fontStyle: "italic"},
  {tag: CM.tags.strong, fontWeight: "700"},
  {tag: CM.tags.strikethrough, textDecoration: "line-through", color: "var(--nb-dimmer)"},
  {tag: [CM.tags.link, CM.tags.url], color: "var(--nb-accent)"},
  {tag: CM.tags.monospace, fontFamily: "var(--nb-mono)", fontSize: "0.92em", color: "var(--nb-week)"},
  {tag: CM.tags.quote, color: "var(--nb-dim)"},
  {tag: [CM.tags.processingInstruction, CM.tags.meta, CM.tags.labelName], color: "var(--nb-dimmer)"},
  {tag: CM.tags.contentSeparator, color: "var(--nb-dimmer)"},
  {tag: CM.tags.comment, color: "var(--nb-dimmer)", fontStyle: "italic"},
]);

// The frontmatter block folds on its own; the properties show in the header.
const frontmatterFold = CM.foldService.of((state, lineStart) => {
  if(lineStart !== 0) return null;
  const doc = state.doc;
  if(doc.lines < 3 || doc.line(1).text.trim() !== "---") return null;
  for(let i = 2; i <= Math.min(doc.lines, 80); i++){
    if(doc.line(i).text.trim() === "---") return {from: doc.line(1).to, to: doc.line(i).to};
  }
  return null;
});

class TaskWidget extends CM.WidgetType {
  constructor(done){ super(); this.done = done; }
  eq(o){ return o.done === this.done; }
  toDOM(){
    const el = document.createElement("input");
    el.type = "checkbox"; el.className = "cm-task"; el.checked = this.done; el.tabIndex = -1;
    return el;
  }
  ignoreEvent(){ return false; }
}

// Wikilinks and checkbox lines, decorated over the visible part of the note.
function decorations(resolveKnown){
  return CM.ViewPlugin.fromClass(class {
    constructor(view){ this.deco = this.build(view); }
    update(u){ if(u.docChanged || u.viewportChanged) this.deco = this.build(u.view); }
    build(view){
      const b = new CM.RangeSetBuilder();
      const doc = view.state.doc;
      const marks = [];
      for(const {from, to} of view.visibleRanges){
        let pos = from;
        while(pos <= to){
          const line = doc.lineAt(pos);
          const t = line.text;
          const m = TASK_RE.exec(t);
          if(m){
            const a = line.from + m[1].length;
            const done = m[2] !== " ";
            marks.push({from: a, to: a + 3, deco: CM.Decoration.replace({widget: new TaskWidget(done)})});
            if(done && a + 4 < line.to) marks.push({from: a + 4, to: line.to, deco: CM.Decoration.mark({class: "cm-task-done"})});
          }
          WIKI_RE.lastIndex = 0;
          let w;
          while((w = WIKI_RE.exec(t))){
            const target = w[1].split("|")[0].split("#")[0].trim();
            const known = resolveKnown ? resolveKnown(target) : true;
            marks.push({from: line.from + w.index, to: line.from + w.index + w[0].length,
                        deco: CM.Decoration.mark({class: "cm-wikilink" + (known === false ? " missing" : ""),
                                                  attributes: {"data-target": w[1]}})});
          }
          if(line.to >= to) break;
          pos = line.to + 1;
        }
      }
      marks.sort((x, y) => x.from - y.from || x.to - y.to);
      let last = -1;
      for(const m of marks){
        if(m.from < last) continue;
        b.add(m.from, m.to, m.deco);
        last = m.to;
      }
      return b.finish();
    }
  }, {decorations: v => v.deco});
}

// Toggle the checkbox on every selected line; a plain list line gains one.
function toggleTasks(view){
  const changes = [];
  const seen = new Set();
  for(const r of view.state.selection.ranges){
    const a = view.state.doc.lineAt(r.from).number, z = view.state.doc.lineAt(r.to).number;
    for(let n = a; n <= z; n++){
      if(seen.has(n)) continue;
      seen.add(n);
      const line = view.state.doc.line(n);
      const m = TASK_RE.exec(line.text);
      if(m){
        const p = line.from + m[1].length + 1;
        changes.push({from: p, to: p + 1, insert: m[2] === " " ? "x" : " "});
      }else{
        const l = LIST_RE.exec(line.text);
        if(l) changes.push({from: line.from + l[1].length, insert: "[ ] "});
        else if(line.text.trim()) changes.push({from: line.from, insert: "- [ ] "});
      }
    }
  }
  if(!changes.length) return false;
  view.dispatch({changes});
  return true;
}

function flipAt(view, pos){
  const line = view.state.doc.lineAt(pos);
  const m = TASK_RE.exec(line.text);
  if(!m) return false;
  const p = line.from + m[1].length + 1;
  view.dispatch({changes: {from: p, to: p + 1, insert: m[2] === " " ? "x" : " "}});
  return true;
}

function insertAtCursor(view, text){
  const r = view.state.selection.main;
  view.dispatch({changes: {from: r.from, to: r.to, insert: text}, selection: {anchor: r.from + text.length}});
  view.focus();
}

// [[ offers notes; ![[ offers notes and files.
function wikiCompletion(complete){
  return async ctx => {
    const m = ctx.matchBefore(/(!?)\[\[([^\]\[|#\n]*)$/);
    if(!m) return null;
    const embed = m.text.startsWith("!");
    const q = m.text.slice(embed ? 3 : 2);
    if(!q && !ctx.explicit) return null;
    let items = [];
    try{ items = await complete(q, embed ? "all" : "note"); }catch(e){ items = []; }
    return {
      from: m.from + (embed ? 3 : 2),
      filter: false,
      options: items.map(i => ({
        label: i.label, detail: i.detail, type: i.kind === "file" ? "constant" : "text",
        apply: (view, c, from, to) => {
          const after = view.state.sliceDoc(to, to + 2);
          const ins = i.insert + (after === "]]" ? "" : "]]");
          const end = from + ins.length + (after === "]]" ? 2 : 0);
          view.dispatch({changes: {from, to, insert: ins}, selection: {anchor: end}});
        }
      }))
    };
  };
}

// opts: {text, mtime, editable, complete(q, kind), attach(file), openNote(target),
//        resolveKnown(target) -> true|false|undefined, save(text, mtime) -> {mtime, saved},
//        onDirty(bool), onStatus(text, cls), onConflict(payload)}
export function mountEditor(host, opts){
  let dirty = false, saving = false, timer = null, mtime = opts.mtime, destroyed = false;
  let savedDoc = opts.text;

  const status = (t, cls) => opts.onStatus && opts.onStatus(t, cls || "");

  async function save(force){
    if(destroyed || saving || !opts.editable) return false;
    const text = view.state.doc.toString();
    if(!force && text === savedDoc){ dirty = false; opts.onDirty && opts.onDirty(false); status("saved", "ok"); return true; }
    saving = true; status("saving");
    try{
      const r = await opts.save(text, mtime, force === "overwrite");
      mtime = r.mtime;
      savedDoc = text;
      dirty = view.state.doc.toString() !== savedDoc;
      opts.onDirty && opts.onDirty(dirty);
      status(dirty ? "unsaved" : "saved", dirty ? "" : "ok");
      if(dirty) arm();
      return true;
    }catch(e){
      if(e.status === 409 && opts.onConflict){ status("changed on disk", "bad"); opts.onConflict(e.payload || {}); }
      else if(e.status === 400 && e.payload && e.payload.secret && opts.onSecret){ status("not saved", "bad"); opts.onSecret(e.payload); }
      else status("save failed: " + e.message, "bad");
      return false;
    }finally{
      saving = false;
    }
  }
  function arm(){ clearTimeout(timer); timer = setTimeout(() => save(false), AUTOSAVE_MS); }

  const updateListener = CM.EditorView.updateListener.of(u => {
    if(!u.docChanged) return;
    const now = u.state.doc.toString() !== savedDoc;
    if(now !== dirty){ dirty = now; opts.onDirty && opts.onDirty(dirty); }
    status(dirty ? "unsaved" : "saved", dirty ? "" : "ok");
    if(dirty) arm();
  });

  const domHandlers = CM.EditorView.domEventHandlers({
    mousedown(e, view){
      const t = e.target;
      if(t.classList && t.classList.contains("cm-task")){
        e.preventDefault();
        if(!opts.editable) return true;
        const pos = view.posAtDOM(t);
        flipAt(view, pos);
        return true;
      }
      const wl = t.closest && t.closest(".cm-wikilink");
      if(wl && (e.ctrlKey || e.metaKey || e.altKey)){
        e.preventDefault();
        opts.openNote && opts.openNote(wl.dataset.target);
        return true;
      }
      return false;
    },
    paste(e, view){
      if(!opts.editable || !opts.attach) return false;
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for(const it of items){
        if(it.kind === "file"){
          const f = it.getAsFile();
          if(!f) continue;
          e.preventDefault();
          status("uploading");
          opts.attach(f).then(r => { insertAtCursor(view, r.wikilink); status("unsaved"); })
                        .catch(err => status("upload failed: " + err.message, "bad"));
          return true;
        }
      }
      return false;
    },
    drop(e, view){
      if(!opts.editable || !opts.attach) return false;
      const files = (e.dataTransfer && e.dataTransfer.files) || [];
      if(!files.length) return false;
      e.preventDefault();
      const pos = view.posAtCoords({x: e.clientX, y: e.clientY});
      if(pos != null) view.dispatch({selection: {anchor: pos}});
      (async () => {
        for(const f of files){
          try{ const r = await opts.attach(f); insertAtCursor(view, r.wikilink); }
          catch(err){ status("upload failed: " + err.message, "bad"); }
        }
      })();
      return true;
    }
  });

  const keys = CM.Prec.highest(CM.keymap.of([
    {key: "Mod-s", run: () => { save(true); return true; }},
    {key: "Mod-l", run: toggleTasks},
    {key: "Mod-Enter", run: toggleTasks},
  ]));

  const state = CM.EditorState.create({
    doc: opts.text,
    extensions: [
      theme, CM.syntaxHighlighting(highlight),
      CM.markdown({base: CM.markdownLanguage, codeLanguages: []}),
      CM.history(), CM.drawSelection(), CM.dropCursor(), CM.highlightActiveLine(), CM.highlightSelectionMatches(),
      CM.closeBrackets(), CM.bracketMatching(), CM.indentOnInput(),
      CM.codeFolding({placeholderText: "properties"}), frontmatterFold,
      CM.autocompletion({override: [wikiCompletion(opts.complete || (async () => []))], icons: false,
                         activateOnTyping: true, defaultKeymap: false}),
      decorations(opts.resolveKnown),
      CM.EditorView.lineWrapping,
      CM.EditorState.readOnly.of(!opts.editable),
      CM.EditorView.editable.of(!!opts.editable),
      keys,
      CM.keymap.of([...CM.closeBracketsKeymap, ...CM.defaultKeymap, ...CM.searchKeymap, ...CM.historyKeymap,
                    ...CM.foldKeymap, ...CM.completionKeymap, ...CM.markdownKeymap, CM.indentWithTab]),
      updateListener, domHandlers,
      CM.placeholder(opts.editable ? "write" : ""),
    ]
  });
  const view = new CM.EditorView({state, parent: host});

  // Fold the frontmatter straight away.
  if(view.state.doc.lines >= 3 && view.state.doc.line(1).text.trim() === "---"){
    for(let i = 2; i <= Math.min(view.state.doc.lines, 80); i++){
      if(view.state.doc.line(i).text.trim() === "---"){
        view.dispatch({effects: CM.foldEffect.of({from: view.state.doc.line(1).to, to: view.state.doc.line(i).to})});
        break;
      }
    }
  }
  status(opts.editable ? "saved" : "read only", opts.editable ? "ok" : "");

  return {
    view,
    save: () => save(true),
    flush: async () => { clearTimeout(timer); return dirty ? await save(true) : true; },
    isDirty: () => dirty,
    text: () => view.state.doc.toString(),
    // After a conflict: take the disk version, or push ours over it.
    reload(text, newMtime){
      mtime = newMtime; savedDoc = text; dirty = false;
      view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: text}});
      opts.onDirty && opts.onDirty(false); status("reloaded", "ok");
    },
    overwrite(newMtime){ mtime = newMtime; return save("overwrite"); },
    allowSecret(){ return opts.save(view.state.doc.toString(), mtime, false, true).then(r => { mtime = r.mtime; savedDoc = view.state.doc.toString(); dirty = false; opts.onDirty && opts.onDirty(false); status("saved", "ok"); }); },
    insert: t => insertAtCursor(view, t),
    focus: () => view.focus(),
    destroy(){ destroyed = true; clearTimeout(timer); view.destroy(); },
  };
}
