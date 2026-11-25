# web/vendor/cm.js

CodeMirror 6, bundled once into a single ES module so the page keeps having no
build step. Rebuild only to pick up a new CodeMirror version.

    mkdir cmbuild && cd cmbuild && npm init -y
    npm install codemirror @codemirror/lang-markdown @codemirror/autocomplete @codemirror/commands ^
        @codemirror/search @codemirror/state @codemirror/view @codemirror/language ^
        @lezer/highlight @lezer/markdown esbuild
    (write entry.js, below)
    npx esbuild entry.js --bundle --format=esm --minify --target=es2022 --legal-comments=none ^
        --outfile=web/vendor/cm.js

entry.js re-exports what `ui\editor.js` imports:

    // Everything the note editor needs from CodeMirror 6, bundled into one ES module.
    export {EditorState, StateField, StateEffect, Compartment, RangeSetBuilder, Prec, Text, EditorSelection} from "@codemirror/state";
    export {EditorView, keymap, Decoration, WidgetType, ViewPlugin, drawSelection, dropCursor, placeholder,
            highlightActiveLine, highlightSpecialChars, rectangularSelection, lineNumbers} from "@codemirror/view";
    export {defaultKeymap, history, historyKeymap, indentWithTab, undo, redo} from "@codemirror/commands";
    export {syntaxHighlighting, HighlightStyle, defaultHighlightStyle, foldGutter, foldKeymap, foldService, foldEffect,
            codeFolding, syntaxTree, indentOnInput, bracketMatching, foldAll, unfoldAll} from "@codemirror/language";
    export {markdown, markdownLanguage, markdownKeymap, insertNewlineContinueMarkup, deleteMarkupBackward} from "@codemirror/lang-markdown";
    export {autocompletion, startCompletion, closeCompletion, closeBrackets, closeBracketsKeymap, completionKeymap,
            acceptCompletion} from "@codemirror/autocomplete";
    export {searchKeymap, highlightSelectionMatches, openSearchPanel, search} from "@codemirror/search";
    export {tags} from "@lezer/highlight";

Versions in the current bundle (2026-09-23):

    @codemirror/autocomplete@6.20.3
    @codemirror/commands@6.11.1
    @codemirror/lang-markdown@6.5.2
    @codemirror/language@6.12.4
    @codemirror/search@6.7.2
    @codemirror/state@6.7.6
    @codemirror/view@6.43.13
    @lezer/highlight@1.2.3
    @lezer/markdown@1.7.2
    cmbuild@1.0.0
    codemirror@6.0.2
    esbuild@0.28.2

Bundle size: 594710 bytes.
